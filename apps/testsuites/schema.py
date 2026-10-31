from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from rest_framework import serializers

from .serializers import TestCaseSummarySerializer


class TestSuiteDeleteBodyAutoSchema(AutoSchema):
    def _get_request_body(self, direction='request'):
        if self.method != 'DELETE':
            return super()._get_request_body(direction)
        method = self.method
        self.method = 'POST'
        try:
            return super()._get_request_body(direction)
        finally:
            self.method = method


_ID_SCHEMA = {'oneOf': [
    {'type': 'integer', 'minimum': 1},
    {'type': 'string', 'pattern': '^[1-9][0-9]*$'},
]}


@extend_schema_field({
    'oneOf': [
        _ID_SCHEMA,
        {
            'type': 'object',
            'properties': {'id': _ID_SCHEMA},
            'required': ['id'],
        },
    ],
    'description': 'A positive test case ID, or an object containing its id.',
})
class TestSuiteSingleCaseIdInputField(serializers.Field):
    def to_internal_value(self, data):
        return data

    def to_representation(self, value):
        return value


@extend_schema_field({
    'oneOf': [
        {
            'type': 'array',
            'items': {
                'oneOf': [
                    _ID_SCHEMA,
                    {
                        'type': 'object',
                        'properties': {'id': _ID_SCHEMA},
                        'required': ['id'],
                    },
                    {
                        'type': 'object',
                        'properties': {'test_case_id': _ID_SCHEMA},
                        'required': ['test_case_id'],
                    },
                    {
                        'type': 'object',
                        'properties': {'testcase_id': _ID_SCHEMA},
                        'required': ['testcase_id'],
                    },
                ],
            },
        },
        _ID_SCHEMA,
    ],
    'description': 'A list of case IDs or one scalar ID. Batch aliases also accept ID objects.',
})
class TestSuiteCaseIdsInputField(serializers.Field):
    def to_internal_value(self, data):
        return data

    def to_representation(self, value):
        return value


@extend_schema_field({
    'oneOf': [
        {
            'type': 'object',
            'properties': {
                'test_case_id': {'type': 'integer', 'minimum': 1},
                'order': {'type': 'integer', 'minimum': 0},
            },
            'required': ['test_case_id', 'order'],
        },
        {
            'type': 'object',
            'properties': {
                'testcase_id': {'type': 'integer', 'minimum': 1},
                'order': {'type': 'integer', 'minimum': 0},
            },
            'required': ['testcase_id', 'order'],
        },
    ],
    'description': 'One test case membership and its non-negative execution order.',
})
class TestSuiteCaseOrderItemInputField(serializers.Field):
    def to_internal_value(self, data):
        return data

    def to_representation(self, value):
        return value


class TestSuiteAddCaseByTestCaseIdRequestSerializer(serializers.Serializer):
    test_case_id = TestSuiteSingleCaseIdInputField()


class TestSuiteAddCaseByTestcaseIdRequestSerializer(serializers.Serializer):
    testcase_id = TestSuiteSingleCaseIdInputField()


TEST_SUITE_SINGLE_CASE_REQUEST = PolymorphicProxySerializer(
    component_name='TestSuiteSingleCaseAddRequest',
    serializers=(
        TestSuiteAddCaseByTestCaseIdRequestSerializer,
        TestSuiteAddCaseByTestcaseIdRequestSerializer,
    ),
    resource_type_field_name=None,
)


class TestSuiteAddCasesByTestCaseIdsRequestSerializer(serializers.Serializer):
    test_case_ids = TestSuiteCaseIdsInputField()


class TestSuiteAddCasesByTestcaseIdsRequestSerializer(serializers.Serializer):
    testcase_ids = TestSuiteCaseIdsInputField()


class TestSuiteAddCasesByTestcasesRequestSerializer(serializers.Serializer):
    testcases = TestSuiteCaseIdsInputField()


TEST_SUITE_BATCH_CASES_REQUEST = PolymorphicProxySerializer(
    component_name='TestSuiteBatchCasesAddRequest',
    serializers=(
        TestSuiteAddCasesByTestCaseIdsRequestSerializer,
        TestSuiteAddCasesByTestcaseIdsRequestSerializer,
        TestSuiteAddCasesByTestcasesRequestSerializer,
    ),
    resource_type_field_name=None,
)


class TestSuiteReorderByTestCaseOrdersRequestSerializer(serializers.Serializer):
    test_case_orders = serializers.ListField(child=TestSuiteCaseOrderItemInputField())


class TestSuiteReorderByTestcaseOrdersRequestSerializer(serializers.Serializer):
    testcase_orders = serializers.ListField(child=TestSuiteCaseOrderItemInputField())


TEST_SUITE_REORDER_REQUEST = PolymorphicProxySerializer(
    component_name='TestSuiteCaseReorderRequest',
    serializers=(
        TestSuiteReorderByTestCaseOrdersRequestSerializer,
        TestSuiteReorderByTestcaseOrdersRequestSerializer,
    ),
    resource_type_field_name=None,
)


class TestSuiteCaseMembershipResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    testcase = TestCaseSummarySerializer()
    testcase_id = serializers.IntegerField()
    test_case_id = serializers.IntegerField()
    order = serializers.IntegerField()


class TestSuiteCaseBatchAddResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    added = serializers.IntegerField()
    testcases = TestSuiteCaseMembershipResponseSerializer(many=True)


class TestSuiteCaseReorderResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    updated = serializers.IntegerField()
    testcases = TestSuiteCaseMembershipResponseSerializer(many=True)


class TestSuiteCaseRemoveResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    deleted = serializers.BooleanField()
    test_case_id = serializers.IntegerField()


class TestSuiteCaseNotFoundResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class TestSuiteCaseValidationErrorResponseSerializer(serializers.Serializer):
    test_case_id = serializers.CharField(required=False)
    test_case_ids = serializers.CharField(required=False)
    test_case_orders = serializers.CharField(required=False)


class TestSuiteCaseAuthFailureResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
