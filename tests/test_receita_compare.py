from experiments.receita_compare import clean_url, selected


def test_trace_excludes_credentials_and_query_strings():
    assert clean_url("https://example.com/path?token=secret") == "https://example.com/path"
    assert selected({"Cookie": "secret", "x-captcha-token": "secret",
                     "Location": "https://example.com/next?token=secret"}) == {
        "location": "https://example.com/next"
    }
