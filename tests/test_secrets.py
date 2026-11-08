import pytest
from pydantic import ValidationError

from boatyard.models.config_file import SecretsDeclaration


def test_validate_consumed_passes_on_subsets() -> None:
    declaration = SecretsDeclaration.model_validate(["PUBLIC_URL", "GRAFANA_PORT"])

    declaration.validate_consumed({"PUBLIC_URL"}, "k8s", "secrets.yaml")


def test_validate_consumed_names_rendering_and_undeclared_keys() -> None:
    declaration = SecretsDeclaration.model_validate(["S3_ACCESS_KEY"])
    with pytest.raises(ValueError, match=r"compose rendering consumes undeclared keys.*GRAFANA_PORT, LOKI_PORT"):
        declaration.validate_consumed({"GRAFANA_PORT", "LOKI_PORT"}, "compose", "secrets.yaml")


def test_an_empty_file_parses_as_the_empty_declaration() -> None:
    empties: tuple[object, ...] = (None, [])
    for empty in empties:
        declaration = SecretsDeclaration.model_validate(empty)

        assert declaration.root == []
        declaration.validate_consumed(set(), "k8s", "secrets.yaml")
        with pytest.raises(ValueError, match="undeclared keys: S3_ACCESS_KEY"):
            declaration.validate_consumed({"S3_ACCESS_KEY"}, "k8s", "secrets.yaml")


def test_a_non_list_declaration_refuses() -> None:
    with pytest.raises(ValidationError):
        SecretsDeclaration.model_validate({"PUBLIC_URL": {"description": "x"}})


def test_a_duplicate_key_refuses() -> None:
    with pytest.raises(ValidationError, match="'PUBLIC_URL' is declared twice"):
        SecretsDeclaration.model_validate(["PUBLIC_URL", "PUBLIC_URL"])


def test_a_non_caps_name_refuses() -> None:
    with pytest.raises(ValidationError, match="must be an ALL_CAPS env name"):
        SecretsDeclaration.model_validate(["public_url"])
