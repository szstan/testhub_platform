import pytest

from apps.executions.datasets import DatasetError, expand_dataset


@pytest.mark.parametrize(
    'matrix',
    [
        {'account': []},
        {'account': ['active'], 'region': []},
    ],
)
def test_dataset_expansion_rejects_empty_matrix_columns(matrix):
    with pytest.raises(DatasetError, match='empty'):
        expand_dataset({'matrix': matrix})


def test_dataset_expansion_keeps_default_variant_when_matrix_is_absent():
    assert expand_dataset({}) == [{}]
    assert expand_dataset({'matrix': {}}) == [{}]
