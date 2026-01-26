def test_web_search_fact(concierge_client):
    """Test performing a live web search for a known fact."""

    query = "What is the capital of France?"
    # We ask for a specific format to make assertion easier
    course_id = concierge_client.dispatch(f"Search the web to find out: {query}")
    result = concierge_client.wait_for_completion(course_id)

    # Basic keyword check
    assert "Paris" in result, f"Expected 'Paris' in search result, got: '{result}'"
