from pathlib import Path
import pytest

from dicom_repack import parser


def test_help_output_documents_every_option(capsys):
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in (
        "--fileFilter",
        "--outputType",
        "--version",
        "--help",
    ):
        assert flag in out
