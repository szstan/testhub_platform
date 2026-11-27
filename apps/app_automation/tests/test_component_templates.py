from copy import deepcopy

import pytest

from apps.app_automation.component_templates import (
    ExpansionLimitError,
    MissingParameterError,
    TemplateResolutionError,
    expand_steps,
    validate_steps,
)


def _definition(project_id, component_type, version, steps, **extra):
    return {
        "project_id": project_id,
        "type": component_type,
        "version": version,
        "steps": steps,
        **extra,
    }


def _resolver(definitions):
    def resolve(project_id, component_type, version):
        return definitions.get((project_id, component_type, version))

    return resolve


def test_nested_reuse_substitutes_parameters_and_preserves_step_metadata():
    definitions = {
        ("p1", "type_text", "2"): _definition(
            "p1",
            "type_text",
            "2",
            [{
                "type": "input",
                "config": {"text": "{{params.text}}"},
                "metadata": {"source": "shared", "ordinal": 0},
            }],
            parameters={"text": {"type": "string", "required": True}},
        ),
        ("p1", "login", "1"): _definition(
            "p1",
            "login",
            "1",
            [
                {
                    "type": "set_variable",
                    "config": {"value": "{{params.message}}"},
                    "metadata": {"owner": "outer"},
                },
                {"kind": "custom", "type": "type_text", "version": "2", "params": {"text": "{{params.message}}"}},
                {
                    "kind": "custom",
                    "type": "type_text",
                    "version": "2",
                    "params": {"text": "repeat {{params.message}}"},
                },
            ],
            parameters={"message": {"type": "string", "required": True}},
        ),
    }
    source_steps = [{
        "kind": "custom",
        "type": "login",
        "version": "1",
        "params": {"message": "hello"},
    }]

    expanded = expand_steps(source_steps, project_id="p1", resolve_template=_resolver(definitions))

    assert [step["type"] for step in expanded] == ["set_variable", "input", "input"]
    assert expanded[0]["config"]["value"] == "hello"
    assert expanded[0]["metadata"] == {"owner": "outer"}
    assert [step["config"]["text"] for step in expanded[1:]] == ["hello", "repeat hello"]
    assert expanded[1]["metadata"] == {"source": "shared", "ordinal": 0}
    assert expanded[1]["metadata"] is not definitions[("p1", "type_text", "2")]["steps"][0]["metadata"]


def test_defaults_exact_placeholders_preserve_falsy_types_and_runner_tokens():
    definitions = {
        ("p1", "defaults", "1"): _definition(
            "p1",
            "defaults",
            "1",
            [{
                "type": "set_variable",
                "config": {
                    "zero": "{{params.zero}}",
                    "disabled": "{{params.disabled}}",
                    "empty": "{{params.empty}}",
                    "nothing": "{{params.nothing}}",
                    "interpolated": "count={{params.zero}}, enabled={{params.disabled}}",
                    "runner_value": "{{local.value}}",
                },
            }],
            parameters={
                "zero": {"type": "integer", "required": True, "default": 0},
                "disabled": {"type": "boolean", "default": False},
                "empty": {"type": "string", "default": ""},
                "nothing": {"type": "null", "default": None},
            },
        )
    }

    expanded = expand_steps(
        [{"kind": "custom", "type": "defaults", "version": "1"}],
        project_id="p1",
        resolve_template=_resolver(definitions),
    )

    config = expanded[0]["config"]
    assert config["zero"] == 0 and type(config["zero"]) is int
    assert config["disabled"] is False
    assert config["empty"] == ""
    assert config["nothing"] is None
    assert config["interpolated"] == "count=0, enabled=false"
    assert config["runner_value"] == "{{local.value}}"


def test_existing_schema_and_default_config_declarations_are_supported():
    definitions = {
        ("p1", "legacy_definition", "3"): _definition(
            "p1",
            "legacy_definition",
            "3",
            [{"type": "wait", "duration": "{{params.duration}}"}],
            schema={
                "properties": {"duration": {"type": "number"}},
                "required": ["duration"],
            },
            default_config={"duration": 0},
        )
    }

    assert expand_steps(
        [{"kind": "custom", "type": "legacy_definition", "version": "3"}],
        project_id="p1",
        resolve_template=_resolver(definitions),
    ) == [{"type": "wait", "duration": 0}]


def test_legacy_inline_custom_steps_use_config_as_params_without_resolving():
    calls = []
    source = [{
        "kind": "custom",
        "type": "legacy_login",
        "config": {"username": ""},
        "metadata": {"saved_by": "old-editor"},
        "steps": [{
            "type": "input",
            "config": {"text": "{{params.username}}"},
            "metadata": {"inline": True},
        }],
    }]

    expanded = expand_steps(
        source,
        project_id="p1",
        resolve_template=lambda *args: calls.append(args),
    )

    assert calls == []
    assert expanded == [{
        "type": "input",
        "config": {"text": ""},
        "metadata": {"inline": True},
    }]


def test_missing_required_parameter_has_clear_name_without_echoing_values():
    definitions = {
        ("p1", "needs_name", "1"): _definition(
            "p1", "needs_name", "1", [{"type": "input"}],
            parameters={"username": {"type": "string", "required": True}},
        )
    }

    with pytest.raises(MissingParameterError, match="required parameter 'username'"):
        expand_steps(
            [{"kind": "custom", "type": "needs_name", "version": "1", "params": {}}],
            project_id="p1",
            resolve_template=_resolver(definitions),
        )


def test_resolver_result_from_another_project_is_refused():
    calls = []
    wrong_project_definition = _definition("p2", "shared", "1", [{"type": "wait"}])

    def resolver(project_id, component_type, version):
        calls.append((project_id, component_type, version))
        return wrong_project_definition

    with pytest.raises(TemplateResolutionError, match="authorized project scope"):
        expand_steps(
            [{"kind": "custom", "type": "shared", "version": "1"}],
            project_id="p1",
            resolve_template=resolver,
        )
    assert calls == [("p1", "shared", "1")]


def test_cycle_and_recursion_depth_are_bounded():
    cycle = {
        ("p1", "a", "1"): _definition(
            "p1", "a", "1", [{"kind": "custom", "type": "b", "version": "1"}]
        ),
        ("p1", "b", "1"): _definition(
            "p1", "b", "1", [{"kind": "custom", "type": "a", "version": "1"}]
        ),
    }
    with pytest.raises(TemplateResolutionError, match="cycle detected"):
        expand_steps(
            [{"kind": "custom", "type": "a", "version": "1"}],
            project_id="p1",
            resolve_template=_resolver(cycle),
        )

    chain = {
        ("p1", "a", "1"): _definition(
            "p1", "a", "1", [{"kind": "custom", "type": "b", "version": "1"}]
        ),
        ("p1", "b", "1"): _definition("p1", "b", "1", [{"type": "wait"}]),
    }
    with pytest.raises(ExpansionLimitError, match="depth limit"):
        expand_steps(
            [{"kind": "custom", "type": "a", "version": "1"}],
            project_id="p1",
            resolve_template=_resolver(chain),
            max_depth=1,
        )


def test_expansion_enforces_step_count_and_serialized_payload_limits():
    with pytest.raises(ExpansionLimitError, match="step count"):
        expand_steps(
            [{"type": "wait"}, {"type": "wait"}],
            project_id="p1",
            resolve_template=_resolver({}),
            max_steps=1,
        )

    definitions = {
        ("p1", "repeat", "1"): _definition(
            "p1", "repeat", "1",
            [{"type": "input", "config": {"text": "{{params.text}}"}} for _ in range(4)],
            parameters={"text": {"type": "string", "required": True}},
        )
    }
    with pytest.raises(ExpansionLimitError, match="cumulative"):
        expand_steps(
            [{"kind": "custom", "type": "repeat", "version": "1", "params": {"text": "x" * 50}}],
            project_id="p1",
            resolve_template=_resolver(definitions),
            max_payload_bytes=700,
        )


def test_expansion_never_mutates_source_steps_or_template_definitions():
    definitions = {
        ("p1", "copy", "1"): _definition(
            "p1", "copy", "1",
            [{"type": "set_variable", "config": {"value": "{{params.value}}"}, "metadata": {"tags": ["safe"]}}],
            parameters={"value": {"type": "object", "required": True}},
        )
    }
    source = [{"kind": "custom", "type": "copy", "version": "1", "params": {"value": {"ok": False}}}]
    original_source = deepcopy(source)
    original_definitions = deepcopy(definitions)

    expanded = expand_steps(source, project_id="p1", resolve_template=_resolver(definitions))
    expanded[0]["config"]["value"]["ok"] = True
    expanded[0]["metadata"]["tags"].append("changed")

    assert source == original_source
    assert definitions == original_definitions


def test_project_resolver_accepts_numeric_project_ids():
    definition = _definition(42, "wait_once", "1", [{"type": "wait"}])
    calls = []

    def resolver(project_id, component_type, version):
        calls.append((project_id, component_type, version))
        return definition

    assert expand_steps(
        [{"kind": "custom", "type": "wait_once", "version": "1"}],
        project_id=42,
        resolve_template=resolver,
    ) == [{"type": "wait"}]
    assert calls == [(42, "wait_once", "1")]


def test_validate_steps_accepts_nested_runner_steps_and_rejects_bad_shapes():
    validate_steps([{"type": "loop", "steps": [{"type": "wait"}]}])

    with pytest.raises(ValueError, match="type"):
        validate_steps([{"name": "missing type"}])

    with pytest.raises(ValueError, match="version is required"):
        validate_steps([{"kind": "custom", "type": "unversioned"}])


def test_config_control_flow_lists_expand_in_place_and_preserve_source():
    child_reference = {
        "kind": "custom",
        "type": "child",
        "version": "1",
        "params": {"text": "{{params.text}}"},
    }
    definitions = {
        ("p1", "child", "1"): _definition(
            "p1",
            "child",
            "1",
            [
                {"type": "input", "config": {"text": "{{params.text}}"}},
                {"type": "input", "config": {"text": "{{params.text}}"}},
            ],
            parameters={"text": {"type": "string", "required": True}},
        ),
        ("p1", "flow", "1"): _definition(
            "p1",
            "flow",
            "1",
            [{
                "type": "branch",
                "config": {
                    "steps": [{"type": "loop", "config": {"steps": [child_reference]}}],
                    "then_steps": [child_reference],
                    "else_steps": [{"type": "wait"}],
                    "try_steps": [],
                    "catch_steps": [child_reference],
                    "finally_steps": [{"type": "wait"}],
                },
            }],
            parameters={"text": {"type": "string", "required": True}},
        ),
    }
    source = [{
        "kind": "custom",
        "type": "flow",
        "version": "1",
        "params": {"text": "typed"},
    }]
    original_source = deepcopy(source)
    original_definitions = deepcopy(definitions)

    expanded = expand_steps(source, project_id="p1", resolve_template=_resolver(definitions))

    assert len(expanded) == 1
    config = expanded[0]["config"]
    assert config["steps"][0]["config"]["steps"] == [
        {"type": "input", "config": {"text": "typed"}},
        {"type": "input", "config": {"text": "typed"}},
    ]
    assert config["then_steps"] == [
        {"type": "input", "config": {"text": "typed"}},
        {"type": "input", "config": {"text": "typed"}},
    ]
    assert config["else_steps"] == [{"type": "wait"}]
    assert config["try_steps"] == []
    assert config["catch_steps"] == config["then_steps"]
    assert config["finally_steps"] == [{"type": "wait"}]
    assert source == original_source
    assert definitions == original_definitions


def test_config_control_flow_lists_reject_malformed_shapes():
    with pytest.raises(ValueError, match="config.then_steps must be a list"):
        validate_steps([{"type": "branch", "config": {"then_steps": "not a list"}}])

    with pytest.raises(ValueError, match=r"config.else_steps\[0\] must be an object"):
        validate_steps([{"type": "branch", "config": {"else_steps": [None]}}])


def test_custom_config_arrays_remain_parameter_values():
    definitions = {
        ("p1", "array_parameter", "1"): _definition(
            "p1",
            "array_parameter",
            "1",
            [{"type": "set_variable", "config": {"value": "{{params.steps}}"}}],
            parameters={"steps": {"type": "array", "required": True}},
        )
    }
    supplied = [{"not_a_step": True}]

    expanded = expand_steps(
        [{
            "kind": "custom",
            "type": "array_parameter",
            "version": "1",
            "config": {"steps": supplied},
        }],
        project_id="p1",
        resolve_template=_resolver(definitions),
    )

    assert expanded == [{"type": "set_variable", "config": {"value": supplied}}]
    assert expanded[0]["config"]["value"] is not supplied


def test_nested_config_reference_is_project_scoped():
    outer = _definition(
        "p1",
        "outer",
        "1",
        [{"type": "branch", "config": {
            "then_steps": [{"kind": "custom", "type": "inner", "version": "1"}],
        }}],
    )
    wrong_project = _definition("p2", "inner", "1", [{"type": "wait"}])
    calls = []

    def resolver(project_id, component_type, version):
        calls.append((project_id, component_type, version))
        return outer if component_type == "outer" else wrong_project

    with pytest.raises(TemplateResolutionError, match="authorized project scope"):
        expand_steps(
            [{"kind": "custom", "type": "outer", "version": "1"}],
            project_id="p1",
            resolve_template=resolver,
        )

    assert calls == [("p1", "outer", "1"), ("p1", "inner", "1")]


def test_config_arrays_enforce_recursion_step_payload_and_cycle_limits():
    nested_steps = [{"type": "branch", "config": {
        "then_steps": [{"type": "wait"}, {"type": "wait"}],
    }}]
    with pytest.raises(ExpansionLimitError, match="depth limit"):
        expand_steps(
            nested_steps,
            project_id="p1",
            resolve_template=_resolver({}),
            max_depth=0,
        )
    with pytest.raises(ExpansionLimitError, match="step count"):
        expand_steps(
            nested_steps,
            project_id="p1",
            resolve_template=_resolver({}),
            max_steps=2,
        )

    large_definition = _definition(
        "p1", "large", "1", [{"type": "set_variable", "config": {"value": "x" * 200}}]
    )
    with pytest.raises(ExpansionLimitError, match="cumulative"):
        expand_steps(
            [{"type": "branch", "config": {
                "then_steps": [{"kind": "custom", "type": "large", "version": "1"}],
            }}],
            project_id="p1",
            resolve_template=_resolver({("p1", "large", "1"): large_definition}),
            max_payload_bytes=500,
        )

    cyclic_definition = _definition(
        "p1",
        "cycle",
        "1",
        [{"type": "branch", "config": {
            "try_steps": [{"kind": "custom", "type": "cycle", "version": "1"}],
        }}],
    )
    with pytest.raises(TemplateResolutionError, match="cycle detected"):
        expand_steps(
            [{"kind": "custom", "type": "cycle", "version": "1"}],
            project_id="p1",
            resolve_template=_resolver({("p1", "cycle", "1"): cyclic_definition}),
        )
