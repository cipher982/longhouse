"""Test to verify the behavior of the globally mocked ChatOpenAI."""

# This test relies on the global patching of langchain_openai.ChatOpenAI done in conftest.py.

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI


def test_globally_mocked_chatOpenAI_behavior():
    """Verify the globally mocked ChatOpenAI from conftest works as expected."""
    # Instantiate ChatOpenAI - this will use the _StubChatOpenAI from conftest
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)  # Parameters are ignored by the stub

    # The _StubChatOpenAI.bind_tools returns _StubLlm (also from conftest)
    bound_llm = llm.bind_tools([])
    assert hasattr(bound_llm, "invoke"), "_StubLlm should have an invoke method"

    # Calling invoke on _StubLlm should return a specific AIMessage
    result = bound_llm.invoke([{"type": "human", "content": "Hello"}])

    # Check the result from the _StubLlm in conftest
    assert isinstance(result, AIMessage), "Expected AIMessage from _StubLlm"
    assert result.content == "stub-response", "Expected specific content from _StubLlm"

    # If we reached here without hanging, the basic mock interaction is working.
    print("Globally mocked ChatOpenAI behaved as expected.")
