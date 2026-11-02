import pytest
from pydantic import ValidationError

from docker_devkit.score import AuthoredProvisioner


def test_uri_and_type_are_required() -> None:
    with pytest.raises(ValidationError):
        AuthoredProvisioner.model_validate({"uri": "template://example/x"})

    with pytest.raises(ValidationError):
        AuthoredProvisioner.model_validate({"type": "example-x"})


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="init"):
        AuthoredProvisioner.model_validate({
            "uri": "template://example/x",
            "type": "example-x",
            "init": "echo hello",
        })


def test_string_form_state_is_a_loud_error() -> None:
    with pytest.raises(ValidationError, match=r"string form.*a mapping is required"):
        AuthoredProvisioner.model_validate({
            "uri": "template://example/x",
            "type": "example-x",
            "state": "serviceName: db\n",
        })


def test_string_form_services_is_a_loud_error() -> None:
    with pytest.raises(ValidationError, match="string form"):
        AuthoredProvisioner.model_validate({
            "uri": "template://example/x",
            "type": "example-x",
            "services": "db:\n  image: example/db\n",
        })


def test_string_form_manifests_names_the_list_shape() -> None:
    with pytest.raises(ValidationError, match="a list is required"):
        AuthoredProvisioner.model_validate({
            "uri": "template://example/x",
            "type": "example-x",
            "manifests": "- apiVersion: v1\n",
        })
