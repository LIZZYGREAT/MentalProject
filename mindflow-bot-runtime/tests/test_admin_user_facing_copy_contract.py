from pathlib import Path


APP_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "admin_web"
    / "static"
    / "app.js"
).read_text(encoding="utf-8")


def _section(start: str, end: str) -> str:
    return APP_SOURCE.split(start, 1)[1].split(end, 1)[0]


def test_default_admin_copy_hides_engineering_versions_and_revisions():
    forbidden_default_fragments = (
        "当前版本</span>",
        "VERSION HISTORY",
        "Calendar revision</dt>",
        "Observation revision</dt>",
        "初始状态 revision",
        "个体参数版本：",
        "显式稳定画像 v",
        "revision ${r.revision}",
    )

    for fragment in forbidden_default_fragments:
        assert fragment not in APP_SOURCE


def test_participant_list_uses_business_profile_status_not_version_numbers():
    participants = _section("function participantsView", "function userDetail")

    assert "画像状态" in participants
    assert "profileDisplayStatus" in participants
    assert "profile_version??" not in participants
    assert "learned_profile_version??" not in participants


def test_workload_residual_default_table_has_no_model_version_column():
    workload = _section("function workloadView", "function stage5View")
    default_table = workload.split("renderAuditDetails('workloadResidualAudit'", 1)[0]

    assert "平均残差" in default_table
    assert "workload_model_version',label:'模型版本'" not in default_table
    assert "workloadResidualAudit" in workload


def test_statuses_are_localized_through_shared_display_helpers():
    assert "function displayStatus" in APP_SOURCE
    assert "function displayParameterStatus" in APP_SOURCE
    assert "active:'正常使用'" in APP_SOURCE
    assert "pending:'待发送'" in APP_SOURCE
    assert "sent:'已发送'" in APP_SOURCE
    assert "candidate:'候选参数'" in APP_SOURCE
    assert "shadow:'影子评估'" in APP_SOURCE
    assert "validated:'已通过验证'" in APP_SOURCE


def test_audit_details_keep_raw_json_and_copy_capability():
    assert "function renderAuditDetails" in APP_SOURCE
    assert "原始 JSON" in APP_SOURCE
    assert "Copy JSON" in APP_SOURCE
    assert "data-copy-json" in APP_SOURCE
