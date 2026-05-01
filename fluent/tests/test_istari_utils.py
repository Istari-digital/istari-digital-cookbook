"""Tests for istari_fluent.istari_utils."""

from istari_fluent.istari_utils import _paginate_manually, _next_config_name


class _MockPage:
    def __init__(self, items, pages=1):
        self.items = items
        self.pages = pages


def _make_list_func(all_items, page_size=2):
    def list_func(page=1, size=10, **kwargs):
        start = (page - 1) * size
        end = start + size
        chunk = all_items[start:end]
        total_pages = (len(all_items) + size - 1) // size if all_items else 0
        return _MockPage(items=chunk, pages=max(1, total_pages))
    return list_func


class TestPaginateManually:
    def test_empty(self):
        list_func = _make_list_func([])
        result = _paginate_manually(list_func, page_size=2)
        assert result == []

    def test_single_page(self):
        list_func = _make_list_func([1, 2], page_size=10)
        result = _paginate_manually(list_func, page_size=10)
        assert result == [1, 2]

    def test_multiple_pages(self):
        list_func = _make_list_func([1, 2, 3, 4, 5], page_size=2)
        result = _paginate_manually(list_func, page_size=2)
        assert result == [1, 2, 3, 4, 5]


class TestNextConfigName:
    def test_increments_trailing_digits(self):
        assert _next_config_name("v3") == "v4"
        assert _next_config_name("v12") == "v13"
        assert _next_config_name("config_3") == "config_4"

    def test_appends_timestamp_when_no_digits(self):
        result = _next_config_name("baseline")
        assert result.startswith("baseline_")
        assert len(result) > len("baseline_")
