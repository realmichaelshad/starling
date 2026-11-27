from app.safety import assess_context, assess_draft, safe_source_url
from app.schemas import PostContext


def test_sensitive_protected_context_is_still_blocked():
    assessment = assess_context(PostContext(author="a", text="leak their home address", visibility="protected"))
    assert assessment.blocked
    assert assessment.local_only
    assert not assessment.research_allowed


def test_engagement_bait_is_flagged_without_claiming_it_is_viral():
    assessment = assess_draft("Follow me and repost if you agree")
    assert "engagement_bait" in assessment.categories
    assert assessment.severity == "warning"


def test_source_urls_reject_local_network_targets():
    assert safe_source_url("https://example.com/research")
    assert not safe_source_url("http://127.0.0.1/private")
    assert not safe_source_url("file:///etc/passwd")
