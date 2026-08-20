from app.principal import Principal, PrincipalValidationError, principal_from_client_id


def test_uuid_client_id_normalizes_to_anonymous_principal() -> None:
    principal = principal_from_client_id(" 550e8400-e29b-41d4-a716-446655440000 ")
    assert principal == Principal(type="anonymous_client", id="550e8400-e29b-41d4-a716-446655440000")


def test_invalid_client_id_is_rejected() -> None:
    for value in ("", "a", "../../watchlist", "x" * 65, None):
        try:
            principal_from_client_id(value)
        except PrincipalValidationError:
            continue
        raise AssertionError(f"expected invalid client id: {value!r}")


def test_principal_storage_key_is_stable_and_logs_use_digest() -> None:
    principal = Principal(type="anonymous_client", id="550e8400-e29b-41d4-a716-446655440000")
    assert principal.storage_key == "anonymous_client:550e8400-e29b-41d4-a716-446655440000"
    assert len(principal.log_digest) == 16
    assert principal.id not in principal.log_digest
