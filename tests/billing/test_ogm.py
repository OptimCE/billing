import pytest

from utils import ogm


def test_generate_known_vectors():
    assert ogm.generate(0) == "+++000/0000/00097+++"  # remainder 0 → check 97
    assert ogm.generate(1) == "+++000/0000/00101+++"


def test_generate_remainder_zero_maps_to_97():
    # 97 % 97 == 0 → check digits are 97, never 00
    assert ogm.generate(97).endswith("97+++")


def test_roundtrip_validate():
    for base in (0, 1, 42, 123456, 123456789, 9_999_999_999):
        assert ogm.validate(ogm.generate(base))


def test_validate_rejects_bad_check_and_length():
    assert ogm.validate("+++000/0000/00097+++") is True
    assert ogm.validate("+++000/0000/00098+++") is False  # wrong check for base 0
    assert ogm.validate("+++000/0000/00100+++") is False  # base 1 → check 01, not 00
    assert ogm.validate("12345") is False  # wrong length


def test_generate_out_of_range():
    with pytest.raises(ValueError, match="10-digit"):
        ogm.generate(-1)
    with pytest.raises(ValueError, match="10-digit"):
        ogm.generate(10_000_000_000)
