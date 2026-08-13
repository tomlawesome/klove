from klove.security.auth import BearerAuthenticator, Principal, authorize


def test_bearer_authentication_is_exact_and_fail_closed() -> None:
    authenticator = BearerAuthenticator("a" * 32)

    assert authenticator.authenticate(None) is None
    assert authenticator.authenticate("Basic " + "a" * 32) is None
    assert authenticator.authenticate("Bearer") is None
    assert authenticator.authenticate("Bearer ") is None
    assert authenticator.authenticate("Bearer wrong token") is None
    assert authenticator.authenticate("Bearer " + "b" * 32) is None
    assert authenticator.authenticate("Bearer " + "a" * 32) == Principal()

    control = BearerAuthenticator("a" * 32, scopes=frozenset({"printers:read", "printers:control"}))
    assert control.authenticate("Bearer " + "a" * 32) == Principal(
        scopes=frozenset({"printers:read", "printers:control"})
    )


def test_scope_authorization_requires_identity_and_exact_scope() -> None:
    assert not authorize(None, "printers:read")
    assert not authorize(Principal(), "printers:write")
    assert authorize(Principal(), "printers:read")
