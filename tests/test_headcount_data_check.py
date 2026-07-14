from __future__ import annotations

import pandas as pd

from voucher_audit.checks import _headcount_data_check_aux


def _make_rule(**overrides):
    base = {
        "id": "AUX_HEADCOUNT_DATA_CHECK",
        "name": "人次数据检查",
        "type": "headcount_data_check",
        "scope": "aux_ledger",
        "severity": "错误",
        "description": "",
        "source": {"doc": "", "clause": ""},
        "params": {
            "summary_field": "摘要",
            "voucher_field": "凭证号",
            "month_field": "月",
        },
    }
    base.update(overrides)
    return base


def _make_df(rows):
    # 支持可选的本币列
    if len(rows) > 0 and len(rows[0]) == 4:
        header = ["月", "凭证号", "摘要", "本币"]
    else:
        header = ["月", "凭证号", "摘要"]
    return pd.DataFrame(rows, columns=header)


class TestZYCode:
    def test_zy_code_hits_error(self):
        df = _make_df([[3, "V001", "收Z5Y0款"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert not out.empty
        hits = out[out["问题分类"] == "人次数据填写错误"]
        assert len(hits) == 1
        assert hits.iloc[0]["命中码"].lower() == "z5y0"


class TestYSCode:
    def test_ys_code_hits_error(self):
        df = _make_df([[3, "V002", "付Y22S0费"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert not out.empty
        hits = out[out["问题分类"] == "人次数据填写错误"]
        ys_hits = hits[hits["命中码"].str.lower().str.startswith("y")]
        assert len(ys_hits) == 1


class TestZSSuffixViolation:
    def test_zs_suffix_with_text_is_error(self):
        df = _make_df([[3, "V003", "劳务Z50S20调整"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert not out.empty
        suffix_hits = out[(out["问题分类"] == "人次数据填写错误") & (out["命中码"].str.contains("Z50S20", case=False))]
        assert len(suffix_hits) == 1

    def test_zs_suffix_clean_is_ok(self):
        df = _make_df([[3, "V004", "劳务Z50S20"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        # No suffix violation; also no sign violation (non-red-flush, positive numbers)
        assert out.empty

    def test_zs_suffix_with_dash_is_error(self):
        df = _make_df([[3, "V005", "Z50S20-退"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        suffix_hits = out[(out["问题分类"] == "人次数据填写错误") & (out["命中码"].str.contains("Z50S20", case=False))]
        assert len(suffix_hits) == 1

    def test_zs_suffix_with_allowed_punctuation_is_ok(self):
        df = _make_df([[3, "V006", "Z50S20，测试"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert out.empty


class TestZSSignCompliance:
    def test_red_flush_positive_numbers_is_suspect(self):
        """冲销/红冲场景下 ZS 数字应为负，若为正 → 需确认"""
        df = _make_df([[3, "V007", "冲销Z50S20"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        sign_hits = out[out["问题分类"] == "人次码符号需确认"]
        assert len(sign_hits) == 1
        assert sign_hits.iloc[0]["严重度"] == "需确认"

    def test_red_flush_negative_numbers_is_ok(self):
        """冲销/红冲场景下 Z-50S-20 格式正确 → 不命中"""
        df = _make_df([[3, "V008", "红冲Z-50S-20"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert out.empty

    def test_normal_negative_numbers_is_suspect(self):
        """非冲销/红冲场景下 ZS 数字不应为负 → 需确认"""
        df = _make_df([[3, "V009", "正常Z-50S-20"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        sign_hits = out[out["问题分类"] == "人次码符号需确认"]
        assert len(sign_hits) == 1
        assert sign_hits.iloc[0]["严重度"] == "需确认"

    def test_normal_positive_numbers_is_ok(self):
        """非冲销/红冲场景下 Z50S20 格式正确 → 不命中"""
        df = _make_df([[3, "V010", "正常Z50S20"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert out.empty


class TestTargetMonth:
    def test_only_current_month_checked(self):
        df = _make_df([
            [2, "V_OLD", "Z5Y0旧"],
            [3, "V_NEW", "Z5Y0新"],
        ])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert not out.empty
        assert all(out["摘要"].str.contains("新"))


class TestNoMatch:
    def test_clean_summary_no_hit(self):
        df = _make_df([[3, "V011", "普通摘要无特殊码"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert out.empty


class TestChongPrefix:
    """测试以"冲"开头的凭证识别为冲销场景"""

    def test_chong_prefix_negative_is_ok(self):
        """以"冲"开头 + Z-50S-20（负数）→ 正确，不命中"""
        df = _make_df([[3, "V012", "冲2026-02-28日记账Z-50S-20"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert out.empty

    def test_chong_prefix_positive_is_suspect(self):
        """以"冲"开头 + Z50S20（正数）→ 需确认"""
        df = _make_df([[3, "V013", "冲2026-02-28日记账Z50S20"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        sign_hits = out[out["问题分类"] == "人次码符号需确认"]
        assert len(sign_hits) == 1
        assert sign_hits.iloc[0]["命中原因"].startswith("冲销/红冲场景下")


class TestTiaoZhengWithNegativeCurrency:
    """测试"调整"开头且本币为负数的凭证识别为冲销场景"""

    def test_tiaozheng_negative_currency_negative_code_is_ok(self):
        """以"调整"开头 + 本币负数 + Z-50S-20 → 正确，不命中"""
        df = _make_df([[3, "V014", "调整2026-01-13日记账Z-50S-20", -1000.0]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        assert out.empty

    def test_tiaozheng_negative_currency_positive_code_is_suspect(self):
        """以"调整"开头 + 本币负数 + Z50S20（正数）→ 需确认"""
        df = _make_df([[3, "V015", "调整2026-01-13日记账Z50S20", -1000.0]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        sign_hits = out[out["问题分类"] == "人次码符号需确认"]
        assert len(sign_hits) == 1
        assert sign_hits.iloc[0]["命中原因"].startswith("冲销/红冲场景下")

    def test_tiaozheng_positive_currency_negative_code_is_suspect(self):
        """以"调整"开头 + 本币正数 + Z-50S-20（负数）→ 需确认（视为正常场景）"""
        df = _make_df([[3, "V016", "调整2026-01-13日记账Z-50S-20", 1000.0]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        sign_hits = out[out["问题分类"] == "人次码符号需确认"]
        assert len(sign_hits) == 1
        assert "非冲销/红冲场景" in sign_hits.iloc[0]["命中原因"]

    def test_tiaozheng_no_currency_field_negative_code_is_suspect(self):
        """以"调整"开头 + 无本币列 + Z-50S-20 → 需确认（无法判断本币，视为正常场景）"""
        df = _make_df([[3, "V017", "调整2026-01-13日记账Z-50S-20"]])
        out = _headcount_data_check_aux(df, 3, _make_rule())
        sign_hits = out[out["问题分类"] == "人次码符号需确认"]
        assert len(sign_hits) == 1
        assert "非冲销/红冲场景" in sign_hits.iloc[0]["命中原因"]
