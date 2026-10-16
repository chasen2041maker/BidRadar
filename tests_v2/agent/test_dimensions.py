"""维度表：跨维度比较在结构上就不可能发生。"""

from bidradar.agent.dimensions import DIMENSIONS, PROFILE_FIELDS, Dimension, allowed_fields
from bidradar.modules.workspace.models import PROFILE_FIELDS as WORKSPACE_FIELDS


def test_schedule_never_sees_onsite_constraints() -> None:
    """ "60 日内完成开发"不能拿"不接受连续 60 天驻场"去比：工期维度根本看不到交付限制字段。"""
    assert "delivery_constraints" not in allowed_fields(Dimension.SCHEDULE)
    assert "delivery_constraints" in allowed_fields(Dimension.ONSITE)


def test_facts_and_materials_are_not_matched() -> None:
    assert not DIMENSIONS[Dimension.FACT].matchable
    assert not DIMENSIONS[Dimension.MATERIALS].matchable


def test_every_dimension_is_described_and_fields_exist() -> None:
    assert set(DIMENSIONS) == set(Dimension)
    for spec in DIMENSIONS.values():
        assert spec.description and spec.label
        assert set(spec.profile_fields) <= set(PROFILE_FIELDS)


def test_profile_fields_match_the_workspace_module() -> None:
    """agent 不能 import workspace，字段名是写死的。两边改了一边，这条会失败。"""
    assert set(PROFILE_FIELDS) == set(WORKSPACE_FIELDS)
