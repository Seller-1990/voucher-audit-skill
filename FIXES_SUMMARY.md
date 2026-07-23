# 优化修复摘要

更新日期：2026-07-23

### 后续优化（同日）

1. 加强 `ensure_no_open_workbook`：检测 Excel `~$*.xlsx` 锁文件，并以读写打开探测共享占用。
2. `cleanup` 默认只删 `temp_*`；删除 `凭证审核输出` 需显式 `--include-reports`，CLI 输出分类标签。
3. 补充 `runner` / `report` / `rule_guardrails` / 占用检测测试；覆盖率门槛 35% → 38%。

### 主修复

本轮针对 skill 可发现性、Agent 工作流完整性、标注规则漂移、备份安全性与 CLI 一致性完成以下修复：

1. 重写 `SKILL.md`：补充中文触发词、Agent 标准流程、`inspect/cleanup/AI` 命令、6 条现行规则、确认门与常见失败处理；与 README 对齐。
2. 修复源表标注规则 ID 漂移：`source_annotation` 补齐 `INC_PP_CHANGE` / 负毛利 / 外包等现行规则，并保留历史 PP 规则兼容。
3. 修复备份命名：`backup_file` 使用 `*.xlsx.bak`（保留原扩展名），冲突时追加时间戳；`restore_from_backup` 支持显式备份路径与历史 `*.bak` 回退。
4. 标注失败回滚改为传入真实 `backup_path`，避免默认路径与备份路径不一致。
5. CLI 去掉错误的 `PreviewItem` TypedDict，改用 `preview.PreviewItem` dataclass；`rules set-active` 强制仓库内路径并复用 `update_active_pointer`。
6. `rules_template` 改为运行时从 packaged `default_rules` 组装，避免手写 YAML 与生产规则漂移；保留 legacy addable types 供 guardrails。
7. `__init__.py` 补充版本号；新增 security / annotation specs / template / set-active 测试。

历史修复（2026-07-14）摘要：

1. `cleanup` 改为显式工作目录、默认拒绝删除、`--yes` 确认，并加入路径边界检查。
2. 修复 `rule_patcher.py` 引用不存在模块导致无法导入的问题。
3. 文本读取改为严格 UTF-8，禁止静默忽略解码错误。
4. 修正规则可视化工具硬编码 16 条规则、执行计划错误生成时间和错误默认规则路径。
5. 新增 `pyproject.toml`、console script、可选 AI/dev 依赖和有上限的依赖范围；wheel 内置默认规则并验证独立安装可运行。
6. 将客户一致性、同比波动及对应报告构建从大型模块中拆出。
7. 修复辅助工具中的未定义变量、错误集合写入和批处理路径初始化问题。
8. CI 增加 Windows 单元测试、Ruff、编译检查和 35% 覆盖率门槛。
9. 新增真实 Excel COM 显式集成测试入口。
10. README、交接文档和测试报告统一到当前 6 条规则及实际命令。

历史设计与实施过程已归档到 `docs/workflow-archive/`，不再作为当前操作手册。
