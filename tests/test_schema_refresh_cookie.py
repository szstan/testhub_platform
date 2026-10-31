from django.test import SimpleTestCase, override_settings
from django.urls import include, path
from drf_spectacular.generators import SchemaGenerator

urlpatterns = [
    path('api/auth/', include('apps.users.urls')),
    path('api/users/', include('apps.users.urls')),
]


def resolved(document, value):
    while '$ref' in value:
        value = document['components']['schemas'][value['$ref'].rsplit('/', 1)[-1]]
    return value


@override_settings(ROOT_URLCONF=__name__)
class CookieRefreshSchemaTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = SchemaGenerator(patterns=urlpatterns).get_schema(request=None, public=True)

    def test_cookie_is_a_real_alternative_to_an_optional_write_only_body(self):
        for prefix in ('/api/auth', '/api/users'):
            operation = self.document['paths'][prefix + '/token/refresh/']['post']
            body = operation['requestBody']
            self.assertFalse(body.get('required', False))
            request = resolved(self.document, body['content']['application/json']['schema'])
            self.assertNotIn('refresh', request.get('required', []))
            self.assertTrue(request['properties']['refresh']['writeOnly'])
            cookie = next(item for item in operation['parameters'] if item['in'] == 'cookie' and item['name'] == 'testhub_refresh')
            self.assertFalse(cookie.get('required', False))
            self.assertIn('/api/auth/', cookie['description'])
            self.assertFalse(operation.get('security'))

    def test_rotated_refresh_is_not_exposed_in_json_and_cookie_clearing_is_documented(self):
        for prefix in ('/api/auth', '/api/users'):
            operation = self.document['paths'][prefix + '/token/refresh/']['post']
            response = resolved(self.document, operation['responses']['200']['content']['application/json']['schema'])
            self.assertEqual(set(response['properties']), {'access'})
            self.assertIn('401', operation['responses'])
            self.assertIn('Set-Cookie', operation['responses']['200']['headers'])
            self.assertIn('Set-Cookie', operation['responses']['401']['headers'])
