from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.llm_judge.models import Rubric
from apps.users.models import User


@pytest.mark.django_db
def test_single_score_hides_non_default_rubrics_from_regular_users():
    user = User.objects.create_user(username='single-score-user', password='pass')
    default_rubric = Rubric.objects.create(name='visible-default', is_default=True)
    hidden_rubric = Rubric.objects.create(name='hidden-policy', is_default=False)
    client = APIClient()
    client.force_authenticate(user=user)
    payload = {'question': 'question', 'answer': 'answer'}

    with patch('apps.llm_judge.service.JudgeService.score_single', return_value={'score': 90}) as score:
        denied = client.post(
            '/api/llm-judge/judge/single/',
            {**payload, 'rubric': hidden_rubric.pk},
            format='json',
        )
        assert denied.status_code == 400
        score.assert_not_called()

        allowed = client.post(
            '/api/llm-judge/judge/single/',
            {**payload, 'rubric': default_rubric.pk},
            format='json',
        )

    assert allowed.status_code == 200
    score.assert_called_once()


@pytest.mark.django_db
def test_single_score_allows_staff_to_use_active_non_default_rubric():
    staff = User.objects.create_user(username='single-score-staff', password='pass', is_staff=True)
    rubric = Rubric.objects.create(name='staff-private-policy', is_default=False)
    client = APIClient()
    client.force_authenticate(user=staff)

    with patch('apps.llm_judge.service.JudgeService.score_single', return_value={'score': 80}) as score:
        response = client.post(
            '/api/llm-judge/judge/single/',
            {'question': 'question', 'answer': 'answer', 'rubric': rubric.pk},
            format='json',
        )

    assert response.status_code == 200
    score.assert_called_once()
