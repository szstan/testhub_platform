from django.urls import path

from . import views


urlpatterns = [
    path('', views.TestSuiteListCreateView.as_view(), name='testsuite-list'),
    path('<int:pk>/', views.TestSuiteDetailView.as_view(), name='testsuite-detail'),
    # Both spellings are kept because existing frontend modules use both
    # ``test_cases`` and the model's ``testcases`` naming.
    path(
        '<int:pk>/testcases/',
        views.TestSuiteCaseListView.as_view(),
        name='testsuite-testcases',
    ),
    path(
        '<int:pk>/test_cases/',
        views.TestSuiteCaseListView.as_view(),
        name='testsuite-test-cases',
    ),
    path(
        '<int:pk>/add_test_case/',
        views.TestSuiteAddCaseView.as_view(),
        name='testsuite-add-test-case',
    ),
    path(
        '<int:pk>/add_test_cases/',
        views.TestSuiteAddCasesView.as_view(),
        name='testsuite-add-test-cases',
    ),
    path(
        '<int:pk>/remove_test_case/',
        views.TestSuiteRemoveCaseView.as_view(),
        name='testsuite-remove-test-case',
    ),
    path(
        '<int:pk>/update_test_case_order/',
        views.TestSuiteReorderCasesView.as_view(),
        name='testsuite-update-test-case-order',
    ),
    path(
        '<int:pk>/reorder_testcases/',
        views.TestSuiteReorderCasesView.as_view(),
        name='testsuite-reorder-testcases',
    ),
    path(
        '<int:pk>/testcases/<int:testcase_id>/',
        views.TestSuiteCaseDetailView.as_view(),
        name='testsuite-case-detail',
    ),
]
