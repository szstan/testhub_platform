from django.test import SimpleTestCase
from django.urls import include, path
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema

from apps.llm_judge.urls import urlpatterns as judge_urlpatterns
from apps.mcp.urls import urlpatterns as mcp_urlpatterns


def _only_named(patterns, names):
    return [pattern for pattern in patterns if getattr(pattern, "name", None) in names]


SCHEMA_PATTERNS = [
    path(
        "api/llm-judge/",
        include((
            _only_named(judge_urlpatterns, {
                "llm-judge-single", "llm-judge-dashboard-stats", "llm-judge-config-service",
                "llm-judge-batch-upload", "llm-judge-batch-template",
            }),
            "judge_schema_tests",
        )),
    ),
    path(
        "api/mcp/",
        include((
            _only_named(mcp_urlpatterns, {
                "mcp-tools", "mcp-tool-detail", "mcp-config",
                "mcp-pending-approve", "mcp-pending-reject",
            }),
            "mcp_schema_tests",
        )),
    ),
]


class JudgeMcpSchemaTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator(patterns=SCHEMA_PATTERNS).get_schema(
            request=None, public=True,
        )
        validate_schema(cls.schema)

    def operation(self, path_name, method):
        self.assertIn(path_name, self.schema["paths"])
        operations = self.schema["paths"][path_name]
        self.assertIn(method.lower(), operations)
        return operations[method.lower()]

    def resolve(self, schema):
        while True:
            if "$ref" in schema:
                name = schema["$ref"].rsplit("/", 1)[-1]
                schema = self.schema["components"]["schemas"][name]
                continue
            if "allOf" in schema:
                merged = {key: value for key, value in schema.items() if key != "allOf"}
                required = set(merged.get("required", []))
                properties = dict(merged.get("properties", {}))
                for part in schema["allOf"]:
                    resolved = self.resolve(part)
                    properties.update(resolved.get("properties", {}))
                    required.update(resolved.get("required", []))
                if properties:
                    merged["properties"] = properties
                if required:
                    merged["required"] = sorted(required)
                schema = merged
                continue
            return schema

    def properties(self, schema):
        return self.resolve(schema).get("properties", {})

    def response_schema(self, operation, status_code, media_type="application/json"):
        response = operation["responses"][str(status_code)]
        self.assertIn(media_type, response.get("content", {}))
        return response["content"][media_type]["schema"]

    def assert_statuses(self, operation, *codes):
        expected = {str(code) for code in codes}
        self.assertTrue(
            expected.issubset(operation["responses"]),
            f"Missing declared responses: {expected - set(operation['responses'])}",
        )

    def assert_forbidden_union(self, operation):
        schema = self.resolve(self.response_schema(operation, 403))
        variants = [self.properties(variant) for variant in schema["oneOf"]]
        self.assertTrue(any("error" in fields for fields in variants))
        self.assertTrue(any("detail" in fields for fields in variants))

    def test_schema_contains_only_the_eleven_requested_http_handlers(self):
        expected = {
            "/api/llm-judge/batch/template/": "get",
            "/api/llm-judge/batch/upload/": "post",
            "/api/llm-judge/config/service/": {"get", "post"},
            "/api/llm-judge/dashboard/stats/": "get",
            "/api/llm-judge/judge/single/": "post",
            "/api/mcp/config/": "get",
            "/api/mcp/pending/{id}/approve/": "post",
            "/api/mcp/pending/{id}/reject/": "post",
            "/api/mcp/tools/": "get",
            "/api/mcp/tools/{name}/": "get",
        }
        self.assertEqual(set(self.schema["paths"]), set(expected))
        for path_name, methods in expected.items():
            expected_methods = {methods} if isinstance(methods, str) else methods
            with self.subTest(path=path_name):
                self.assertEqual(set(self.schema["paths"][path_name]), expected_methods)

    def test_judge_template_documents_query_formats_and_binary_media(self):
        operation = self.operation("/api/llm-judge/batch/template/", "get")
        params = {item["name"]: item for item in operation["parameters"]}
        self.assertEqual(set(params), {"tpl", "format"})
        for name in params:
            self.assertEqual(params[name]["in"], "query")
            self.assertFalse(params[name].get("required", False))
            self.assertEqual(set(params[name]["schema"]["enum"]), {"csv", "xlsx", "txt"})
        media = operation["responses"]["200"]["content"]
        self.assertEqual(set(media), {
            "text/csv", "text/plain",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
        for content in media.values():
            schema = self.resolve(content["schema"])
            self.assertEqual(schema["type"], "string")
            self.assertEqual(schema["format"], "binary")
        self.assertEqual(set(operation["responses"]), {"200"})

    def test_judge_upload_requires_a_multipart_binary_file_and_typed_parse_result(self):
        operation = self.operation("/api/llm-judge/batch/upload/", "post")
        body = operation["requestBody"]
        self.assertTrue(body["required"])
        self.assertEqual(set(body["content"]), {"multipart/form-data"})
        request = self.resolve(body["content"]["multipart/form-data"]["schema"])
        self.assertEqual(request["required"], ["file"])
        self.assertEqual(request["properties"]["file"]["format"], "binary")

        response = self.properties(self.response_schema(operation, 200))
        self.assertEqual(set(response), {
            "filename", "total_rows", "valid_rows", "errors", "preview", "cases",
        })
        self.assertEqual(response["errors"]["items"]["type"], "string")
        for key in ("preview", "cases"):
            case = self.properties(response[key]["items"])
            self.assertEqual(set(case), {"question", "answer", "ground_truth", "auto_gt", "context"})
            self.assertTrue({"question", "answer", "auto_gt"}.issubset(
                set(self.resolve(response[key]["items"]).get("required", []))
            ))
        bad_request = self.resolve(self.response_schema(operation, 400))
        variants = [self.properties(variant) for variant in bad_request["oneOf"]]
        self.assertEqual(len(variants), 2)
        self.assertTrue(any("error" in props and "filename" in props for props in variants))
        self.assertTrue(any("detail" in props for props in variants))
        self.assert_statuses(operation, 200, 400, 401, 403)

    def test_single_score_documents_visibility_request_result_and_validation_errors(self):
        operation = self.operation("/api/llm-judge/judge/single/", "post")
        request = self.resolve(operation["requestBody"]["content"]["application/json"]["schema"])
        self.assertEqual(set(request["properties"]), {
            "question", "answer", "ground_truth", "auto_gt", "rubric", "context",
        })
        self.assertEqual(set(request["required"]), {"question", "answer"})
        self.assertEqual(request["properties"]["question"]["maxLength"], 20000)
        self.assertEqual(request["properties"]["answer"]["maxLength"], 50000)
        self.assertFalse({"ground_truth", "auto_gt", "rubric", "context"} & set(request["required"]))
        self.assertIn("active", operation["description"])
        self.assertIn("staff", operation["description"])
        self.assertIn("default", operation["description"])

        response = self.properties(self.response_schema(operation, 200))
        self.assertEqual(set(response), {
            "request_id", "rule_report", "verdict", "dimension_scores", "llm_score",
            "final_score", "overall_label", "gate_zone", "meta", "rule_score", "vetoed",
            "veto_reasons", "rule_findings", "judge_model", "latency_ms", "cache_hit",
            "record_id",
        })
        self.assertEqual(set(self.properties(response["rule_report"])), {
            "findings", "rule_score", "vetoed", "veto_reasons",
        })
        self.assertEqual(set(self.properties(response["verdict"])), {
            "reasoning", "dimensions", "overall_label", "model",
        })
        self.assertEqual(set(self.properties(response["meta"])), {
            "rule_weight", "llm_weight", "veto_reasons", "latency_ms", "cache_hit",
        })

        bad_request = self.resolve(self.response_schema(operation, 400))
        self.assertEqual(len(bad_request["oneOf"]), 3)
        variants = [self.properties(variant) for variant in bad_request["oneOf"]]
        self.assertTrue(any("error" in props for props in variants))
        self.assertTrue(any("detail" in props for props in variants))
        validation = next(props for props in variants if "question" in props)
        self.assertEqual(validation["question"]["type"], "array")
        self.assertEqual(validation["question"]["items"]["type"], "string")
        self.assert_statuses(operation, 200, 400, 401, 403, 503)

    def test_dashboard_query_bounds_envelope_and_invalid_days_error(self):
        operation = self.operation("/api/llm-judge/dashboard/stats/", "get")
        params = {item["name"]: item for item in operation["parameters"]}
        self.assertEqual(set(params), {"days", "rubric"})
        self.assertEqual(params["days"]["schema"]["type"], "integer")
        self.assertEqual(params["days"]["schema"]["default"], 7)
        self.assertIn("clamped", params["days"]["description"])
        self.assertEqual(params["rubric"]["schema"]["type"], "integer")

        response = self.properties(self.response_schema(operation, 200))
        self.assertEqual(set(response), {
            "total", "vetoed_count", "veto_rate", "pass_rate", "zone_distribution",
            "label_distribution", "veto_top5", "daily_trend", "cache_hit_rate",
            "avg_latency_ms", "mean_score", "days",
        })
        self.assertEqual(set(self.properties(response["zone_distribution"])), {"green", "yellow", "red"})
        label_counts = self.properties(response["label_distribution"])
        self.assertEqual(set(label_counts), {
            "excellent", "acceptable", "needs_improvement", "critical_failure",
        })
        self.assertEqual(set(self.resolve(response["label_distribution"]).get("required", [])), set())
        self.assertEqual(set(self.properties(response["veto_top5"]["items"])), {"rule", "count"})
        self.assertEqual(set(self.properties(response["daily_trend"]["items"])), {
            "day", "mean_score", "cnt",
        })
        self.assertEqual(set(self.properties(self.response_schema(operation, 400))), {"error"})
        self.assert_statuses(operation, 200, 400, 401, 403)

    def test_service_config_redacts_key_and_describes_test_action(self):
        config = self.operation("/api/llm-judge/config/service/", "get")
        fields = self.properties(self.response_schema(config, 200))
        self.assertEqual(set(fields), {
            "judge_model", "judge_mock", "n_runs", "rule_llm_fallback", "cache_timeout",
            "has_api_key",
        })
        self.assertEqual(fields["has_api_key"]["type"], "boolean")
        self.assertNotIn("api_key", fields)
        self.assertNotIn("OPENAI_API_KEY", fields)
        self.assert_statuses(config, 200, 401, 403)

        test = self.operation("/api/llm-judge/config/service/", "post")
        self.assertNotIn("requestBody", test)
        self.assertEqual(set(self.properties(self.response_schema(test, 200))), {"ok", "message"})
        self.assertEqual(set(self.properties(self.response_schema(test, 503))), {"ok", "message"})
        self.assert_statuses(test, 200, 401, 403, 503)

    def test_mcp_catalog_and_detail_type_fixed_metadata_dynamic_tool_json(self):
        catalog = self.operation("/api/mcp/tools/", "get")
        fields = self.properties(self.response_schema(catalog, 200))
        self.assertEqual(set(fields), {"summary", "tools"})
        summary = self.properties(fields["summary"])
        self.assertEqual(set(summary), {"total", "by_category", "calls_7d"})
        self.assertEqual(set(self.properties(summary["by_category"])), {
            "read", "preview", "confirm", "approval",
        })
        tool = self.properties(fields["tools"]["items"])
        self.assertEqual(set(tool), {
            "name", "title", "category", "domain", "summary", "annotations", "input_schema",
            "paired_with", "examples", "stats",
        })
        self.assertEqual(set(self.properties(tool["annotations"])), {
            "title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint",
        })
        self.assertEqual(set(self.properties(tool["stats"])), {
            "calls", "success", "success_rate", "avg_duration_ms",
        })
        self.assert_statuses(catalog, 200, 401, 403)

        detail = self.operation("/api/mcp/tools/{name}/", "get")
        detail_tool = self.properties(self.response_schema(detail, 200))
        self.assertEqual(set(detail_tool), set(tool) | {"description"})
        path_params = [p for p in detail["parameters"] if p["in"] == "path"]
        self.assertEqual([(p["name"], p["schema"]["type"]) for p in path_params], [("name", "string")])
        self.assertEqual(set(self.properties(self.response_schema(detail, 404))), {"error"})
        self.assert_statuses(detail, 200, 401, 403, 404)

    def test_mcp_connection_config_documents_only_owner_token_and_header_envelope(self):
        operation = self.operation("/api/mcp/config/", "get")
        fields = self.properties(self.response_schema(operation, 200))
        self.assertEqual(set(fields), {"server_name", "endpoint", "tool_count", "api_key", "auth_headers"})
        self.assertEqual(self.resolve(fields["server_name"])["enum"], ["TestHub"])
        self.assertIn("authenticated user's own", fields["api_key"]["description"])
        self.assertIn("does not return", fields["api_key"]["description"])
        auth_headers = self.properties(fields["auth_headers"])
        self.assertEqual(set(auth_headers), {"api_key", "jwt"})
        api_key_header = self.resolve(auth_headers["api_key"])
        self.assertEqual(set(api_key_header["properties"]), {"x-mcp-api-key"})
        self.assertEqual(api_key_header["required"], ["x-mcp-api-key"])
        jwt_header = self.resolve(auth_headers["jwt"])
        self.assertEqual(set(jwt_header["properties"]), {"Authorization"})
        self.assertEqual(jwt_header["required"], ["Authorization"])
        self.assert_forbidden_union(operation)
        self.assert_statuses(operation, 200, 401, 403)

    def test_mcp_pending_actions_have_approval_controls_and_distinct_results(self):
        approve = self.operation("/api/mcp/pending/{id}/approve/", "post")
        self.assertNotIn("requestBody", approve)
        approve_response = self.properties(self.response_schema(approve, 200))
        self.assertEqual(set(approve_response), {"status", "result"})
        self.assertEqual(self.resolve(approve_response["status"])["enum"], ["approved"])
        self.assertEqual(set(self.properties(self.response_schema(approve, 400))), {"error"})
        self.assertEqual(set(self.properties(self.response_schema(approve, 401))), {"detail"})
        self.assert_forbidden_union(approve)
        approve_pk = [p for p in approve["parameters"] if p["in"] == "path"]
        self.assertEqual([(p["name"], p["schema"]["type"]) for p in approve_pk], [("id", "integer")])
        self.assert_statuses(approve, 200, 400, 401, 403, 404)

        reject = self.operation("/api/mcp/pending/{id}/reject/", "post")
        self.assertNotIn("requestBody", reject)
        response = self.properties(self.response_schema(reject, 200))
        self.assertEqual(set(response), {"status"})
        self.assertEqual(self.resolve(response["status"])["enum"], ["rejected"])
        for code in (400, 404):
            self.assertEqual(set(self.properties(self.response_schema(reject, code))), {"error"})
        self.assertEqual(set(self.properties(self.response_schema(reject, 401))), {"detail"})
        self.assert_forbidden_union(reject)
        self.assert_statuses(reject, 200, 400, 401, 403, 404)
