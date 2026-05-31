from zerg.session_execution_home import SessionExecutionHome
from zerg.session_execution_home import infer_continuation_kind
from zerg.session_execution_home import infer_execution_home
from zerg.session_execution_home import infer_origin_label


def test_infer_execution_home_prefers_explicit_non_unmanaged_value() -> None:
    assert (
        infer_execution_home(
            execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
            continuation_kind="cloud",
            origin_label="Cloud",
            environment="cloud",
        )
        == SessionExecutionHome.MANAGED_LOCAL
    )


def test_infer_execution_home_falls_back_to_continuation_kind_then_origin() -> None:
    assert (
        infer_execution_home(
            execution_home=None,
            continuation_kind="runner",
            origin_label=None,
            environment=None,
        )
        == SessionExecutionHome.MANAGED_HOSTED
    )
    assert (
        infer_execution_home(
            execution_home=None,
            continuation_kind=None,
            origin_label="Cloud",
            environment=None,
        )
        == SessionExecutionHome.CLOUD_TAKEOVER
    )


def test_infer_continuation_kind_derives_from_execution_home() -> None:
    assert (
        infer_continuation_kind(
            continuation_kind=None,
            execution_home=SessionExecutionHome.CLOUD_TAKEOVER.value,
            origin_label=None,
            environment=None,
        )
        == "cloud"
    )
    assert (
        infer_continuation_kind(
            continuation_kind=None,
            execution_home=None,
            origin_label=None,
            environment="local-devbox",
        )
        == "local"
    )


def test_infer_origin_label_prefers_explicit_origin_then_execution_home() -> None:
    assert (
        infer_origin_label(
            origin_label="Cinder",
            environment="cloud",
            device_id="shipper-zerg",
            execution_home=SessionExecutionHome.CLOUD_TAKEOVER.value,
            continuation_kind="cloud",
        )
        == "Cinder"
    )
    assert (
        infer_origin_label(
            origin_label=None,
            environment=None,
            device_id=None,
            execution_home=SessionExecutionHome.MANAGED_HOSTED.value,
            continuation_kind=None,
        )
        == "Hosted"
    )


def test_infer_origin_label_uses_specific_environment_before_device_id() -> None:
    assert (
        infer_origin_label(
            origin_label=None,
            environment="example-mbp",
            device_id="shipper-zerg",
            execution_home=None,
            continuation_kind=None,
        )
        == "example-mbp"
    )


def test_infer_origin_label_falls_back_to_device_id_then_local() -> None:
    assert (
        infer_origin_label(
            origin_label=None,
            environment="development",
            device_id="shipper-example",
            execution_home=None,
            continuation_kind=None,
        )
        == "example"
    )
    assert (
        infer_origin_label(
            origin_label=None,
            environment="test",
            device_id=None,
            execution_home=None,
            continuation_kind=None,
        )
        == "test"
    )
    assert (
        infer_origin_label(
            origin_label=None,
            environment=None,
            device_id=None,
            execution_home=None,
            continuation_kind=None,
        )
        == "Local"
    )
