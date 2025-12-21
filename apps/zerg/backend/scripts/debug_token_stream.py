#!/usr/bin/env python3
"""Comprehensive Token Stream Configuration Debugging Script.

This script systematically tests the LLM_TOKEN_STREAM configuration at each
layer of abstraction to identify exactly where the flag gets lost or incorrectly set.

Usage:
    # Local execution:
    cd apps/zerg/backend
    python debug_token_stream.py

    # Inside Docker container:
    docker exec zerg-backend-1 python /app/zerg/debug_token_stream.py
"""

import os
import sys
from pathlib import Path


def print_section(title: str, level: int = 1) -> None:
    """Print a formatted section header."""
    symbols = {1: "=" * 80, 2: "-" * 80, 3: "~" * 80}
    separator = symbols.get(level, "-" * 80)

    print(f"\n{separator}")
    print(f"{'#' * level} {title}")
    print(f"{separator}\n")


def print_result(label: str, value: any, source: str = None) -> None:
    """Print a labeled result with optional source information."""
    print(f"  {label:40s}: {value}")
    if source:
        print(f"  {'Source':40s}: {source}")


def check_layer_1_raw_environment() -> dict:
    """Layer 1 - Check raw OS environment variable."""
    print_section("LAYER 1: Raw OS Environment Variable", level=1)

    raw_value = os.environ.get("LLM_TOKEN_STREAM")

    print_result("LLM_TOKEN_STREAM (raw)", repr(raw_value), "os.environ")
    print_result("Type", type(raw_value).__name__)
    print_result("Truthy evaluation", bool(raw_value))

    # Show all environment variables that might be relevant
    print("\n  Related environment variables:")
    for key, value in sorted(os.environ.items()):
        if any(term in key.upper() for term in ["LLM", "STREAM", "TOKEN", "TESTING", "ENV"]):
            print(f"    {key:30s} = {repr(value)[:60]}")

    return {"raw_value": raw_value, "truthy": bool(raw_value)}


def check_layer_2_dotenv_loading() -> dict:
    """Layer 2 - Check what python-dotenv loads from .env files."""
    print_section("LAYER 2: dotenv Loading", level=1)

    # Detect environment (Docker vs local)
    current_path = Path(__file__).resolve()
    if "/app/" in str(current_path):
        repo_root = current_path.parents[1]  # /app/zerg/... -> /app
        env_context = "Docker container"
    else:
        repo_root = current_path.parents[3]  # apps/zerg/backend/... -> repo root
        env_context = "Local development"

    print_result("Execution context", env_context)
    print_result("Detected repo root", str(repo_root))

    # Check for .env files
    env_files = {
        ".env": repo_root / ".env",
        ".env.test": repo_root / ".env.test",
        "backend/.env": repo_root / "apps" / "zerg" / "backend" / ".env",
    }

    print("\n  .env file locations:")
    for name, path in env_files.items():
        exists = path.exists()
        print(f"    {name:20s}: {path} {'[EXISTS]' if exists else '[MISSING]'}")

        if exists:
            # Try to read LLM_TOKEN_STREAM from the file
            try:
                with open(path) as f:
                    for line in f:
                        if line.strip().startswith("LLM_TOKEN_STREAM"):
                            print(f"      Content: {line.strip()}")
            except Exception as e:
                print(f"      Error reading: {e}")

    # Now actually load with dotenv
    print("\n  Loading with python-dotenv:")
    from dotenv import load_dotenv

    # Determine which env file to load (mimicking config.py logic)
    node_env = os.getenv("NODE_ENV", "development")
    print_result("NODE_ENV", node_env)

    if node_env == "test":
        env_path = repo_root / ".env.test"
        if not env_path.exists():
            env_path = repo_root / ".env"
    else:
        env_path = repo_root / ".env"

    print_result("Selected .env file", str(env_path))
    print_result("File exists", env_path.exists())

    # Capture value before loading
    before_load = os.environ.get("LLM_TOKEN_STREAM")
    print_result("LLM_TOKEN_STREAM before load", repr(before_load))

    # Load the .env file
    if env_path.exists():
        loaded = load_dotenv(env_path, override=True)
        print_result("load_dotenv() returned", loaded)
    else:
        print("  WARNING: No .env file found to load!")

    # Check value after loading
    after_load = os.environ.get("LLM_TOKEN_STREAM")
    print_result("LLM_TOKEN_STREAM after load", repr(after_load))
    print_result("Value changed", before_load != after_load)

    return {
        "before": before_load,
        "after": after_load,
        "changed": before_load != after_load,
        "env_path": str(env_path),
    }


def check_layer_3_settings_object() -> dict:
    """Layer 3 - Check what the Settings dataclass reads."""
    print_section("LAYER 3: Settings Object", level=1)

    # Import the settings module
    try:
        from zerg.config import get_settings

        settings = get_settings()

        print("  Settings object attributes:")
        print_result("_llm_token_stream_default", settings._llm_token_stream_default)
        print_result("Type", type(settings._llm_token_stream_default).__name__)

        # Show the dynamic property
        print("\n  Dynamic property evaluation:")
        print_result("llm_token_stream (property)", settings.llm_token_stream)

        # Show what's in os.environ when property is evaluated
        current_env_value = os.environ.get("LLM_TOKEN_STREAM")
        print_result("os.environ['LLM_TOKEN_STREAM']", repr(current_env_value))

        # Manually evaluate the _truthy logic
        from zerg.config import _truthy
        manual_truthy = _truthy(current_env_value)
        print_result("_truthy(os.environ value)", manual_truthy)

        # Show the property logic result
        expected_result = manual_truthy or settings._llm_token_stream_default
        print_result("Expected property result", expected_result)
        print_result("Actual property result", settings.llm_token_stream)
        print_result("Match", expected_result == settings.llm_token_stream)

        # Show other related settings
        print("\n  Related settings:")
        print_result("testing", settings.testing)
        print_result("environment", settings.environment)
        print_result("log_level", settings.log_level)

        return {
            "default": settings._llm_token_stream_default,
            "property": settings.llm_token_stream,
            "match": expected_result == settings.llm_token_stream,
        }

    except Exception as e:
        print(f"  ERROR importing or using settings: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def check_layer_4_constants_module() -> dict:
    """Layer 4 - Check module-level constant in constants.py."""
    print_section("LAYER 4: Constants Module", level=1)

    try:
        from zerg import constants

        print("  Constants module attributes:")
        print_result("LLM_TOKEN_STREAM", constants.LLM_TOKEN_STREAM)
        print_result("Type", type(constants.LLM_TOKEN_STREAM).__name__)

        # Show when the module was loaded
        print_result("Module file", constants.__file__)

        # Show the _settings object that was captured at import time
        print("\n  Internal _settings object:")
        print_result("_settings object", repr(constants._settings))
        print_result("_settings.llm_token_stream", constants._settings.llm_token_stream)
        print_result("_settings._llm_token_stream_default",
                     constants._settings._llm_token_stream_default)

        # Check if constant matches settings
        from zerg.config import get_settings
        current_settings = get_settings()

        print("\n  Comparison with fresh settings:")
        print_result("Fresh settings.llm_token_stream", current_settings.llm_token_stream)
        print_result("Constants.LLM_TOKEN_STREAM", constants.LLM_TOKEN_STREAM)
        print_result("Match", current_settings.llm_token_stream == constants.LLM_TOKEN_STREAM)

        return {
            "constant": constants.LLM_TOKEN_STREAM,
            "settings_at_import": constants._settings.llm_token_stream,
            "current_settings": current_settings.llm_token_stream,
            "match": current_settings.llm_token_stream == constants.LLM_TOKEN_STREAM,
        }

    except Exception as e:
        print(f"  ERROR importing constants: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def check_layer_5_agent_runner() -> dict:
    """Layer 5 - Check what AgentRunner sees during initialization."""
    print_section("LAYER 5: AgentRunner Initialization", level=1)

    try:
        # We need to create a minimal Agent object
        print("  Creating mock Agent object...")

        from datetime import datetime
        from zerg.models.models import Agent as AgentModel

        # Create a minimal agent for testing
        mock_agent = AgentModel(
            id=999,
            name="Test Agent",
            system_instructions="Test instructions",
            model="gpt-mock",
            allowed_tools=[],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

        print_result("Mock agent created", f"ID={mock_agent.id}, model={mock_agent.model}")

        # Import and create AgentRunner
        from zerg.managers.agent_runner import AgentRunner

        print("\n  Creating AgentRunner instance...")
        runner = AgentRunner(mock_agent)

        print_result("AgentRunner.enable_token_stream", runner.enable_token_stream)
        print_result("Type", type(runner.enable_token_stream).__name__)

        # Show what get_settings() returned during __init__
        from zerg.config import get_settings
        current_settings = get_settings()

        print("\n  Comparison:")
        print_result("Current settings.llm_token_stream", current_settings.llm_token_stream)
        print_result("AgentRunner.enable_token_stream", runner.enable_token_stream)
        print_result("Match", current_settings.llm_token_stream == runner.enable_token_stream)

        # Show cache key that was used
        updated_at_str = mock_agent.updated_at.isoformat()
        cache_key = (mock_agent.id, updated_at_str, runner.enable_token_stream)
        print("\n  Cache information:")
        print_result("Cache key", str(cache_key))

        return {
            "enable_token_stream": runner.enable_token_stream,
            "settings_value": current_settings.llm_token_stream,
            "match": current_settings.llm_token_stream == runner.enable_token_stream,
        }

    except Exception as e:
        print(f"  ERROR creating AgentRunner: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def check_layer_6_llm_creation() -> dict:
    """Layer 6 - Check what _make_llm receives and creates."""
    print_section("LAYER 6: LLM Creation (_make_llm)", level=1)

    try:
        from datetime import datetime
        from zerg.models.models import Agent as AgentModel
        from zerg.agents_def.zerg_react_agent import _make_llm
        from zerg.config import get_settings

        # Create mock agent
        mock_agent = AgentModel(
            id=999,
            name="Test Agent",
            system_instructions="Test instructions",
            model="gpt-mock",
            allowed_tools=[],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

        print("  Calling _make_llm()...")

        # Check settings before calling
        settings = get_settings()
        print_result("settings.llm_token_stream before _make_llm", settings.llm_token_stream)

        # Note: _make_llm needs tools, but we'll pass empty list for testing
        tools = []

        try:
            llm = _make_llm(mock_agent, tools)

            print("\n  LLM object created:")
            print_result("LLM type", type(llm).__name__)
            print_result("LLM class", llm.__class__.__name__)

            # Try to access streaming configuration
            if hasattr(llm, "streaming"):
                print_result("llm.streaming", llm.streaming)

            if hasattr(llm, "model_kwargs"):
                print_result("llm.model_kwargs", llm.model_kwargs)

            # Check the bound object
            if hasattr(llm, "bound"):
                bound_obj = llm.bound
                print("\n  Bound object (ChatOpenAI):")
                print_result("Type", type(bound_obj).__name__)
                if hasattr(bound_obj, "streaming"):
                    print_result("bound.streaming", bound_obj.streaming)
                if hasattr(bound_obj, "model_kwargs"):
                    print_result("bound.model_kwargs", bound_obj.model_kwargs)

            # Try to get the actual ChatOpenAI instance
            actual_llm = llm
            while hasattr(actual_llm, "bound"):
                actual_llm = actual_llm.bound

            print("\n  Underlying ChatOpenAI instance:")
            print_result("Type", type(actual_llm).__name__)

            # Show all attributes that contain "stream"
            print("\n  Attributes containing 'stream':")
            for attr in dir(actual_llm):
                if "stream" in attr.lower() and not attr.startswith("_"):
                    try:
                        value = getattr(actual_llm, attr)
                        if not callable(value):
                            print_result(f"  {attr}", value)
                    except:
                        pass

            return {
                "llm_created": True,
                "llm_type": type(llm).__name__,
                "has_streaming_attr": hasattr(actual_llm, "streaming"),
                "streaming_value": getattr(actual_llm, "streaming", None),
            }

        except Exception as e:
            print(f"  ERROR creating LLM: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    except Exception as e:
        print(f"  ERROR in layer 6: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def check_layer_7_invocation_time() -> dict:
    """Layer 7 - Check what happens at actual invocation time."""
    print_section("LAYER 7: Request/Invocation Time", level=1)

    print("  NOTE: This layer would require hooking into an actual request.")
    print("  To test this layer, you would need to:")
    print("    1. Set a breakpoint in agent_runner.py:run_thread()")
    print("    2. Make a real API request to /agents/{id}/threads/{id}/messages")
    print("    3. Inspect the values at invocation time")

    print("\n  Key locations to inspect during actual request:")
    print("    - agent_runner.py:79  -> self.enable_token_stream = get_settings().llm_token_stream")
    print("    - zerg_react_agent.py:300 -> enable_token_stream = get_settings().llm_token_stream")
    print("    - zerg_react_agent.py:48  -> enable_token_stream = get_settings().llm_token_stream")
    print("    - zerg_react_agent.py:61  -> 'streaming': enable_token_stream in kwargs")

    return {"status": "manual_testing_required"}


def generate_summary(results: dict) -> None:
    """Generate a summary of all findings."""
    print_section("SUMMARY & ANALYSIS", level=1)

    print("  Configuration Flow:")
    print("  " + "=" * 76)

    # Layer 1
    layer1 = results.get("layer1", {})
    raw_value = layer1.get("raw_value")
    print(f"\n  Layer 1 (OS Environment):")
    print(f"    Raw value: {repr(raw_value)}")
    print(f"    Status: {'✓ SET' if raw_value else '✗ NOT SET'}")

    # Layer 2
    layer2 = results.get("layer2", {})
    before = layer2.get("before")
    after = layer2.get("after")
    print(f"\n  Layer 2 (dotenv Loading):")
    print(f"    Before load: {repr(before)}")
    print(f"    After load:  {repr(after)}")
    print(f"    Status: {'✓ LOADED' if layer2.get('changed') else '- UNCHANGED'}")

    # Layer 3
    layer3 = results.get("layer3", {})
    if "error" not in layer3:
        print(f"\n  Layer 3 (Settings Object):")
        print(f"    Default:  {layer3.get('default')}")
        print(f"    Property: {layer3.get('property')}")
        print(f"    Status: {'✓ CONSISTENT' if layer3.get('match') else '✗ MISMATCH'}")

    # Layer 4
    layer4 = results.get("layer4", {})
    if "error" not in layer4:
        print(f"\n  Layer 4 (Constants Module):")
        print(f"    Constant:         {layer4.get('constant')}")
        print(f"    Current settings: {layer4.get('current_settings')}")
        print(f"    Status: {'✓ CONSISTENT' if layer4.get('match') else '✗ MISMATCH'}")

    # Layer 5
    layer5 = results.get("layer5", {})
    if "error" not in layer5:
        print(f"\n  Layer 5 (AgentRunner):")
        print(f"    enable_token_stream: {layer5.get('enable_token_stream')}")
        print(f"    Settings value:      {layer5.get('settings_value')}")
        print(f"    Status: {'✓ CONSISTENT' if layer5.get('match') else '✗ MISMATCH'}")

    # Layer 6
    layer6 = results.get("layer6", {})
    if "error" not in layer6 and layer6.get("llm_created"):
        print(f"\n  Layer 6 (LLM Creation):")
        print(f"    LLM created:      {layer6.get('llm_created')}")
        print(f"    Streaming attr:   {layer6.get('has_streaming_attr')}")
        print(f"    Streaming value:  {layer6.get('streaming_value')}")
        print(f"    Status: {'✓ OK' if layer6.get('llm_created') else '✗ FAILED'}")

    # Identify problems
    print("\n  " + "=" * 76)
    print("\n  ISSUES DETECTED:")

    issues = []

    if not raw_value:
        issues.append("    ✗ LLM_TOKEN_STREAM not set in environment")

    if not layer2.get("changed") and not raw_value:
        issues.append("    ✗ dotenv did not load LLM_TOKEN_STREAM")

    if layer3.get("error"):
        issues.append(f"    ✗ Settings object error: {layer3.get('error')}")
    elif not layer3.get("match"):
        issues.append("    ✗ Settings property doesn't match expected value")

    if layer4.get("error"):
        issues.append(f"    ✗ Constants module error: {layer4.get('error')}")
    elif not layer4.get("match"):
        issues.append("    ✗ Constants module out of sync with current settings")

    if layer5.get("error"):
        issues.append(f"    ✗ AgentRunner error: {layer5.get('error')}")
    elif not layer5.get("match"):
        issues.append("    ✗ AgentRunner value doesn't match settings")

    if layer6.get("error"):
        issues.append(f"    ✗ LLM creation error: {layer6.get('error')}")
    elif not layer6.get("llm_created"):
        issues.append("    ✗ Failed to create LLM object")

    if not issues:
        print("    ✓ No issues detected - configuration looks correct!")
    else:
        for issue in issues:
            print(issue)

    # Recommendations
    print("\n  " + "=" * 76)
    print("\n  RECOMMENDATIONS:")

    if not raw_value and not after:
        print("    1. Add LLM_TOKEN_STREAM=true to your .env file")
        print("    2. Ensure the .env file is in the correct location")
        print(f"       Expected location: {layer2.get('env_path', 'unknown')}")

    if layer4.get("match") is False:
        print("    1. Restart the application to reload constants module")
        print("    2. Or call constants._refresh_feature_flags() in tests")

    if layer5.get("match") is False:
        print("    1. Check AgentRunner.__init__ is calling get_settings() correctly")
        print("    2. Verify no caching is interfering with flag updates")


def main():
    """Run all diagnostic checks."""
    print_section("Token Stream Configuration Debugger", level=1)
    print("  This script traces LLM_TOKEN_STREAM through all layers of the application.")
    print(f"  Python: {sys.version}")
    print(f"  Script: {__file__}")

    results = {}

    # Run all layers
    try:
        results["layer1"] = check_layer_1_raw_environment()
    except Exception as e:
        print(f"\nERROR in Layer 1: {e}")
        results["layer1"] = {"error": str(e)}

    try:
        results["layer2"] = check_layer_2_dotenv_loading()
    except Exception as e:
        print(f"\nERROR in Layer 2: {e}")
        results["layer2"] = {"error": str(e)}

    try:
        results["layer3"] = check_layer_3_settings_object()
    except Exception as e:
        print(f"\nERROR in Layer 3: {e}")
        results["layer3"] = {"error": str(e)}

    try:
        results["layer4"] = check_layer_4_constants_module()
    except Exception as e:
        print(f"\nERROR in Layer 4: {e}")
        results["layer4"] = {"error": str(e)}

    try:
        results["layer5"] = check_layer_5_agent_runner()
    except Exception as e:
        print(f"\nERROR in Layer 5: {e}")
        results["layer5"] = {"error": str(e)}

    try:
        results["layer6"] = check_layer_6_llm_creation()
    except Exception as e:
        print(f"\nERROR in Layer 6: {e}")
        results["layer6"] = {"error": str(e)}

    try:
        results["layer7"] = check_layer_7_invocation_time()
    except Exception as e:
        print(f"\nERROR in Layer 7: {e}")
        results["layer7"] = {"error": str(e)}

    # Generate summary
    generate_summary(results)

    print("\n" + "=" * 80)
    print("Diagnostic complete!")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
