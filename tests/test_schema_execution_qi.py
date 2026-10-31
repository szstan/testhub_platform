from django.test import SimpleTestCase
from django.urls import include, path
from drf_spectacular.generators import SchemaGenerator

from apps.core.urls import urlpatterns as core_urlpatterns
from apps.executions.urls import urlpatterns as execution_urlpatterns
from apps.quality_intelligence.urls import urlpatterns as quality_urlpatterns


def _only_named(patterns, names):
    return [pattern for pattern in patterns if getattr(pattern, "name", None) in names]


SCHEMA_PATTERNS = [
    path(
        "api/executions/",
        include((
            _only_named(execution_urlpatterns, {
                "ci-webhook", "result-import", "execution-metrics", "execution-evidence-explorer",
            }),
            "execution_schema_tests",
        )),
    ),
    path(
        "api/quality-intelligence/",
        include((_only_named(quality_urlpatterns, {"ingest"}), "quality_schema_tests")),
    ),
    path(
        "api/core/",
        include((_only_named(core_urlpatterns, {"docs-list", "docs-content"}), "core_schema_tests")),
    ),
]


class ExecutionQualityCoreSchemaTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator(patterns=SCHEMA_PATTERNS).get_schema(request=None, public=True)

    def operation(self, path_name, method):
        return self.schema["paths"][path_name][method.lower()]

    def resolve(self, schema):
        while "$ref" in schema or "allOf" in schema:
            if "$ref" in schema:
                name = schema["$ref"].rsplit("/", 1)[-1]
                schema = self.schema["components"]["schemas"][name]
                continue
            merged = {key: value for key, value in schema.items() if key != "allOf"}
            for part in schema["allOf"]:
                merged.update(self.resolve(part))
            schema = merged
        return schema

    def properties(self, schema):
        return self.resolve(schema).get("properties", {})

    def response_schema(self, operation, status_code):
        content = operation["responses"][str(status_code)]["content"]
        media_type = next(iter(content))
        return self.resolve(content[media_type]["schema"])

    def test_schema_covers_only_the_seven_actual_handler_methods(self):
        expected = {
            "/api/executions/import-results/": "post",
            "/api/executions/metrics/": "get",
            "/api/executions/evidence-explorer/": "get",
            "/api/executions/webhooks/{provider}/": "post",
            "/api/quality-intelligence/ingest/": "post",
            "/api/core/docs/": "get",
            "/api/core/docs/content/": "get",
        }
        self.assertEqual(set(self.schema["paths"]), set(expected))
        for path_name, method in expected.items():
            with self.subTest(path=path_name):
                self.assertEqual(set(self.schema["paths"][path_name]), {method})

    def test_result_import_documents_required_multipart_fields_and_results(self):
        operation = self.operation("/api/executions/import-results/", "post")
        content = operation["requestBody"]["content"]
        self.assertEqual(set(content), {"multipart/form-data"})
        request_schema = self.resolve(content["multipart/form-data"]["schema"])
        self.assertTrue({"project_id", "format", "external_run_id", "file"}.issubset(
            set(request_schema["required"])
        ))
        self.assertNotIn("build_reference", request_schema["required"])
        self.assertNotIn("maxLength", request_schema["properties"]["external_run_id"])
        props = request_schema["properties"]
        self.assertEqual(props["file"]["format"], "binary")
        self.assertEqual(self.resolve(props["format"])["enum"], ["junit", "allure"])
        response_props = self.properties(self.response_schema(operation, 201))
        self.assertTrue({
            "job_id", "created", "format", "external_run_id", "total", "passed",
            "failed", "skipped", "matched", "unmatched", "status",
        }.issubset(set(response_props)))
        self.assertEqual(set(operation["responses"]), {
            "200", "201", "400", "401", "403", "404", "415",
        })
        import_error = self.properties(self.response_schema(operation, 400))
        self.assertTrue({"detail", "project_id"}.issubset(set(import_error)))
        self.assertTrue(any("ciServiceBearerAuth" in item for item in operation["security"]))
        self.assertEqual(
            self.schema["components"]["securitySchemes"]["ciServiceBearerAuth"]["scheme"],
            "bearer",
        )

    def test_metrics_parameters_and_actual_envelope_are_explicit(self):
        operation = self.operation("/api/executions/metrics/", "get")
        parameters = {item["name"]: item for item in operation["parameters"]}
        self.assertEqual(set(parameters), {"days", "project"})
        self.assertTrue(all(item["in"] == "query" for item in parameters.values()))
        self.assertEqual(parameters["days"]["schema"]["minimum"], 1)
        self.assertEqual(parameters["days"]["schema"]["maximum"], 365)
        self.assertTrue({"days", "project"}.issubset(
            set(self.properties(self.response_schema(operation, 400)))
        ))
        self.assertFalse(any("ciServiceBearerAuth" in item for item in operation.get("security", [])))
        props = self.properties(self.response_schema(operation, 200))
        self.assertEqual(set(props), {
            "window_days", "total_jobs", "ci_jobs", "ci_association_rate", "unmatched_results",
            "gate_evaluations", "gate_pass_rate", "failure_categories", "flaky_profiles",
            "recent_runs", "route_readiness", "enabled_bindings", "integration_health", "scale",
        })
        self.assertEqual(props["failure_categories"]["additionalProperties"]["type"], "integer")
        recent_run = self.resolve(props["recent_runs"]["items"])
        self.assertEqual(set(recent_run["properties"]), {
            "id", "project", "source_type", "source_id", "build_reference", "status",
            "quality_gate", "created_at", "finished_at", "error_message",
        })
        self.assertEqual(recent_run["properties"]["quality_gate"]["type"], "object")
        scale = self.properties(props["scale"])
        self.assertEqual(set(scale), {
            "execution_jobs", "execution_artifacts", "execution_artifact_bytes",
            "expired_execution_artifacts", "execution_oldest_created_at", "execution_latest_created_at",
            "quality_runs", "quality_evidence", "quality_failures", "quality_failure_clusters",
            "unmapped_quality_runs", "unmapped_quality_clusters", "thresholds", "warnings",
        })
        self.assertEqual(set(self.properties(scale["thresholds"])), {
            "execution_jobs", "execution_artifacts", "execution_artifact_bytes",
            "quality_runs", "quality_evidence",
        })
        self.assertEqual(set(operation["responses"]), {"200", "400", "401"})

    def test_evidence_streams_have_independent_filters_pagination_and_scan_cap(self):
        operation = self.operation("/api/executions/evidence-explorer/", "get")
        parameters = {item["name"]: item for item in operation["parameters"]}
        self.assertTrue({
            "artifact_page", "quality_page", "artifact_page_size", "quality_page_size",
            "media_type", "evidence_type", "availability",
        }.issubset(set(parameters)))
        self.assertTrue(all(parameters[name]["in"] == "query" for name in parameters))
        self.assertIn("100,000", operation["responses"]["400"]["description"])
        evidence_error = self.properties(self.response_schema(operation, 400))
        self.assertIn("page", evidence_error)
        self.assertEqual(evidence_error["page"]["type"], "array")
        response_props = self.properties(self.response_schema(operation, 200))
        self.assertEqual(set(response_props), {
            "execution_artifacts", "quality_evidence", "counts", "pagination",
        })
        artifact_item = self.resolve(response_props["execution_artifacts"]["items"])
        quality_item = self.resolve(response_props["quality_evidence"]["items"])
        self.assertEqual(self.resolve(artifact_item["properties"]["metadata"])["type"], "object")
        self.assertEqual(self.resolve(quality_item["properties"]["provenance"])["type"], "object")
        self.assertEqual(set(self.properties(response_props["counts"])), {
            "execution_artifacts", "quality_evidence",
        })
        pagination = self.properties(response_props["pagination"])
        for stream in ("execution_artifacts", "quality_evidence"):
            with self.subTest(stream=stream):
                self.assertEqual(set(self.properties(pagination[stream])), {
                    "total", "page", "page_size", "has_next",
                })
        self.assertEqual(set(operation["responses"]), {"200", "400", "401", "404"})

    def test_ci_webhook_documents_payload_headers_auth_and_controlled_results(self):
        operation = self.operation("/api/executions/webhooks/{provider}/", "post")
        content = operation["requestBody"]["content"]
        self.assertEqual(set(content), {
            "application/json", "application/x-www-form-urlencoded", "multipart/form-data",
        })
        self.assertEqual(self.resolve(content["application/json"]["schema"])["type"], "object")
        parameters = {(item["in"], item["name"]): item for item in operation["parameters"]}
        self.assertTrue(parameters[("path", "provider")]["required"])
        self.assertEqual(set(parameters[("path", "provider")]["schema"]["enum"]), {
            "github", "gitlab", "jenkins", "generic",
        })
        self.assertTrue(parameters[("header", "X-TestHub-Profile")]["required"])
        self.assertIn("an event ID header", operation["description"])
        self.assertIn("timestamp must be within five minutes", operation["description"])
        for header in (
            "X-TestHub-Event", "X-GitHub-Delivery", "X-Gitlab-Event-UUID", "X-Request-ID",
            "X-Hub-Signature-256", "X-TestHub-Signature", "X-TestHub-Timestamp",
        ):
            self.assertIn(("header", header), parameters)
        self.assertTrue(all(not requirement for requirement in operation.get("security", [{}])))
        props = self.properties(self.response_schema(operation, 202))
        self.assertEqual(set(props), {"event_id", "created", "job_id", "status"})
        self.assertEqual(set(operation["responses"]), {
            "200", "202", "400", "401", "404", "415",
        })
        self.assertIn("detail", self.properties(self.response_schema(operation, 401)))

    def test_ingest_documents_input_choices_run_result_and_errors(self):
        operation = self.operation("/api/quality-intelligence/ingest/", "post")
        content = operation["requestBody"]["content"]
        self.assertTrue({"application/json", "application/x-www-form-urlencoded", "multipart/form-data"}.issubset(
            set(content)
        ))
        request_schema = self.resolve(content["application/json"]["schema"])
        self.assertEqual(set(request_schema["required"]), {"source_type", "source_execution_id"})
        self.assertEqual(self.resolve(request_schema["properties"]["source_type"])["enum"], [
            "API", "UI", "APP", "PERF",
        ])
        props = self.properties(self.response_schema(operation, 201))
        self.assertEqual(set(props), {"ingestion", "run"})
        self.assertEqual(set(self.properties(props["ingestion"])), {
            "source_type", "source_execution_id", "quality_run_id", "quality_status", "created",
            "updated", "reused", "source_missing", "dry_run", "warnings",
        })
        run = self.resolve(props["run"])
        self.assertEqual(self.resolve(run["properties"]["summary"])["type"], "object")
        self.assertEqual(self.resolve(run["properties"]["provenance"])["type"], "object")
        self.assertEqual(run["properties"]["warnings"]["type"], "array")
        self.assertEqual(set(operation["responses"]), {
            "200", "201", "400", "401", "403", "404", "409", "500",
        })
        ingest_error = self.properties(self.response_schema(operation, 400))
        self.assertTrue({"error_code", "detail", "fields"}.issubset(set(ingest_error)))

    def test_docs_list_and_content_have_exact_shapes_and_documented_errors(self):
        listing = self.operation("/api/core/docs/", "get")
        list_props = self.properties(self.response_schema(listing, 200))
        self.assertEqual(set(list_props), {"items", "total"})
        self.assertEqual(set(self.properties(list_props["items"]["items"])), {
            "name", "title", "size", "updated_at",
        })
        self.assertEqual(self.properties(list_props["items"]["items"])["updated_at"]["type"], "integer")
        self.assertEqual(set(listing["responses"]), {"200", "401"})

        content = self.operation("/api/core/docs/content/", "get")
        name = next(item for item in content["parameters"] if item["name"] == "name")
        self.assertEqual(name["in"], "query")
        self.assertTrue(name["required"])
        self.assertEqual(set(self.properties(self.response_schema(content, 200))), {
            "name", "title", "content",
        })
        self.assertEqual(set(content["responses"]), {"200", "400", "401", "404", "500"})
        self.assertIn("error", self.properties(self.response_schema(content, 400)))
