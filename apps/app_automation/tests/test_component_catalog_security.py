from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.app_automation.models import AppComponent, AppComponentPackage


class AppComponentCatalogSecurityTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username='catalog-user', password='test-password')
        self.admin = user_model.objects.create_user(
            username='catalog-admin', password='test-password', is_staff=True
        )
        self.component = AppComponent.objects.create(name='Original', type='existing')
        self.package = AppComponentPackage.objects.create(name='Existing package', manifest={})
        self.client = APIClient()

    def test_regular_user_cannot_mutate_global_components_or_packages(self):
        self.client.force_authenticate(user=self.user)

        component_url = f'/api/app-automation/components/{self.component.pk}/'
        package_url = f'/api/app-automation/component-packages/{self.package.pk}/'
        self.assertEqual(
            self.client.patch(component_url, {'name': 'Changed'}, format='json').status_code,
            403,
        )
        self.assertEqual(self.client.delete(component_url).status_code, 403)
        self.assertEqual(
            self.client.patch(package_url, {'name': 'Changed'}, format='json').status_code,
            403,
        )
        self.assertEqual(self.client.delete(package_url).status_code, 403)
        self.assertEqual(
            self.client.post(
                '/api/app-automation/component-packages/',
                {'manifest': {'components': []}},
                format='json',
            ).status_code,
            403,
        )

        self.component.refresh_from_db()
        self.package.refresh_from_db()
        self.assertEqual(self.component.name, 'Original')
        self.assertEqual(self.package.name, 'Existing package')

    def test_invalid_component_package_rolls_back_prior_component_updates(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(
            '/api/app-automation/component-packages/',
            {
                'manifest': {
                    'name': 'Broken package',
                    'components': [
                        {'type': 'existing', 'name': 'Changed'},
                        {'name': 'Missing component type'},
                    ],
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.component.refresh_from_db()
        self.assertEqual(self.component.name, 'Original')
        self.assertFalse(AppComponentPackage.objects.filter(name='Broken package').exists())
