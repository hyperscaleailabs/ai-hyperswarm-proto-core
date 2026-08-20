from hsai import verify


def _block(*lines: str) -> str:
    body = "\n".join(lines)
    return f"Some prose from the model.\n\n{verify.START}\n{body}\n{verify.END}\n"


def test_parse_claim_extracts_commands_and_exit_codes():
    text = _block("ruff check .: exit 0", "pytest: exit 0")
    claim = verify.parse_claim(text)
    assert claim is not None
    assert claim.commands == (("ruff check .", 0), ("pytest", 0))
    assert claim.ok is True


def test_parse_claim_is_none_when_the_block_is_absent():
    assert verify.parse_claim("just some prose, no verification block") is None
    assert verify.parse_claim("") is None


def test_parse_claim_is_none_when_the_block_has_no_parseable_lines():
    text = _block("I ran the tests and they passed, trust me")
    assert verify.parse_claim(text) is None


def test_parse_claim_ok_is_false_on_any_nonzero_exit():
    text = _block("ruff check .: exit 0", "pytest: exit 1")
    claim = verify.parse_claim(text)
    assert claim is not None
    assert claim.ok is False


def test_compare_verified_agree_when_claim_and_ci_both_pass():
    text = _block("ruff check .: exit 0", "pytest: exit 0")
    status, claim = verify.compare(text, ci_ok=True)
    assert status == verify.VERIFIED_AGREE
    assert claim is not None


def test_compare_verified_agree_when_claim_and_ci_both_fail():
    text = _block("ruff check .: exit 0", "pytest: exit 1")
    status, claim = verify.compare(text, ci_ok=False)
    assert status == verify.VERIFIED_AGREE


def test_compare_verified_disagree_when_worker_claims_green_but_ci_is_red():
    """The scenario the acceptance criteria calls out by name: a worker
    claiming green while `ci.run_local` is red."""
    text = _block("ruff check .: exit 0", "pytest: exit 0")
    status, claim = verify.compare(text, ci_ok=False)
    assert status == verify.VERIFIED_DISAGREE
    assert claim is not None and claim.ok is True


def test_compare_verified_disagree_when_worker_claims_red_but_ci_is_green():
    text = _block("pytest: exit 1")
    status, claim = verify.compare(text, ci_ok=True)
    assert status == verify.VERIFIED_DISAGREE


def test_compare_unverified_on_a_missing_block():
    status, claim = verify.compare("no block here", ci_ok=True)
    assert status == verify.UNVERIFIED
    assert claim is None


def test_prompt_instructions_name_the_exact_delimiters():
    assert verify.START in verify.PROMPT_INSTRUCTIONS
    assert verify.END in verify.PROMPT_INSTRUCTIONS
