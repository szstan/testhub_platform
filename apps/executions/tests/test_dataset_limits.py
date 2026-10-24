import pytest

from apps.executions.datasets import DatasetError, expand_dataset


def test_dataset_expansion_rejects_cartesian_product_over_budget():
    with pytest.raises(DatasetError, match='expansion exceeds'):
        expand_dataset({'matrix': {'a': list(range(101)), 'b': list(range(101))}}, max_variants=10000)


def test_dataset_expansion_rejects_oversized_rows_and_columns():
    with pytest.raises(DatasetError, match='column/row limits'):
        expand_dataset({'matrix': {'a': list(range(1001))}})
    with pytest.raises(DatasetError, match='column/row limits'):
        expand_dataset({'matrix': {str(index): [index] for index in range(51)}})


def test_dataset_expansion_rejects_invalid_budget():
    with pytest.raises(DatasetError, match='positive integer'):
        expand_dataset({'data': [{'id': 1}]}, max_variants=0)
