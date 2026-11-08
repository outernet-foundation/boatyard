import pytest
from pydantic import ValidationError

from boatyard.models.config_file import SecretKey, SecretsDeclaration


def test_secret_key_rejects_empty_descriptions() -> None:
    with pytest.raises(ValidationError, match="description must be non-empty"):
        SecretKey.model_validate({"description": "   "})


def test_secret_key_rejects_unknown_spec_keys() -> None:
    with pytest.raises(ValidationError):
        SecretKey.model_validate({"description": "x", "rotation": "weekly"})


def test_validate_consumed_passes_on_subsets() -> None:
    declaration = SecretsDeclaration.model_validate({
        "PUBLIC_URL": {"description": "x", "mint": True},
        "GRAFANA_PORT": {"description": "y"},
    })

    declaration.validate_consumed({"PUBLIC_URL"}, "k8s", "secrets.yaml")


def test_validate_consumed_names_rendering_and_undeclared_keys() -> None:
    declaration = SecretsDeclaration.model_validate({"S3_ACCESS_KEY": {"description": "x"}})
    with pytest.raises(ValueError, match=r"compose rendering consumes undeclared keys.*GRAFANA_PORT, LOKI_PORT"):
        declaration.validate_consumed({"GRAFANA_PORT", "LOKI_PORT"}, "compose", "secrets.yaml")


def test_an_empty_file_parses_as_the_empty_declaration() -> None:
    empties: tuple[object, ...] = (None, {})
    for empty in empties:
        declaration = SecretsDeclaration.model_validate(empty)

        assert declaration.root == {}
        declaration.validate_consumed(set(), "k8s", "secrets.yaml")
        with pytest.raises(ValueError, match="undeclared keys: S3_ACCESS_KEY"):
            declaration.validate_consumed({"S3_ACCESS_KEY"}, "k8s", "secrets.yaml")


def test_a_non_mapping_declaration_refuses() -> None:
    with pytest.raises(ValidationError, match="must be a KEY: description mapping"):
        SecretsDeclaration.model_validate(["PUBLIC_URL"])
