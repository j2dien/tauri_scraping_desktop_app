import sys
import types
import unittest


# openpyxl adalah dependency runtime aplikasi, tetapi venv test minimal tidak
# memasangnya. Stub ini cukup untuk menguji sanitizer tanpa membuat workbook.
if "openpyxl" not in sys.modules:
    openpyxl = types.ModuleType("openpyxl")
    openpyxl.__path__ = []
    openpyxl.Workbook = type("Workbook", (), {})

    styles = types.ModuleType("openpyxl.styles")
    for name in ("Font", "PatternFill", "Alignment", "Border", "Side"):
        setattr(styles, name, type(name, (), {"__init__": lambda self, *args, **kwargs: None}))

    utils = types.ModuleType("openpyxl.utils")
    utils.get_column_letter = lambda value: str(value)

    sys.modules["openpyxl"] = openpyxl
    sys.modules["openpyxl.styles"] = styles
    sys.modules["openpyxl.utils"] = utils


from core.exporter import _optional_count_for_excel, clean_cell_value


class ExcelCellSafetyTests(unittest.TestCase):
    def test_formula_prefixes_are_exported_as_plain_text(self):
        for value in (
            "=HYPERLINK(\"https://example.invalid\")",
            "+1+1",
            "-2+3",
            "@SUM(A1:A2)",
            "   =1+1",
        ):
            with self.subTest(value=value):
                self.assertTrue(clean_cell_value(value).startswith("'"))

    def test_normal_text_is_unchanged_and_control_characters_are_removed(self):
        self.assertEqual(clean_cell_value("komentar biasa"), "komentar biasa")
        self.assertEqual(clean_cell_value("abc\x00def"), "abcdef")

    def test_optional_count_helper_also_sanitizes_untrusted_strings(self):
        self.assertEqual(_optional_count_for_excel(None), "Tidak tersedia")
        self.assertEqual(_optional_count_for_excel("=1+1"), "'=1+1")


if __name__ == "__main__":
    unittest.main()
