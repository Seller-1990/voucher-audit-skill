from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Optional

from .logging_util import Logger, make_logger
from .security import backup_file, restore_from_backup, ensure_no_open_workbook
from .source_annotation import QueryTableAnnotationPlan, SourceAnnotationBundle


HIGHLIGHT_COLOR = 10284031  # RGB(255, 235, 156)
XL_PATTERN_NONE = -4142
XL_CALC_MANUAL = -4135


@dataclass(frozen=True)
class AnnotationWriteResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class ExcelComProbeResult:
    ok: bool
    message: str
    details: tuple[str, ...] = ()


def _load_com_modules() -> tuple[object, object]:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception as e:
        raise RuntimeError(f"Excel COM 不可用：{type(e).__name__}: {e}") from e
    return pythoncom, win32com.client


def _coinitialize(pythoncom: object) -> None:
    try:
        mode = getattr(pythoncom, "COINIT_APARTMENTTHREADED", None)
        if mode is not None and hasattr(pythoncom, "CoInitializeEx"):
            pythoncom.CoInitializeEx(mode)
            return
    except Exception as e:
        text = f"{type(e).__name__}: {e}".lower()
        if "changed mode" not in text and "0x80010106" not in text:
            raise
    pythoncom.CoInitialize()


def _friendly_excel_error_text(action: str, exc: Exception, workbook_path: Optional[Path] = None) -> str:
    raw = f"{type(exc).__name__}: {exc}"
    lower = raw.lower()
    hints: list[str] = []
    if "80070520" in lower or "logon session does not exist" in lower:
        hints.append("请在当前已登录的桌面会话中直接运行；不要在已断开的远程桌面、计划任务或服务账户下调用 Excel COM")
    if "class not registered" in lower or "invalid class string" in lower:
        hints.append("本机可能未安装 Microsoft Excel，或 Office COM 组件未正确注册")
    if "access is denied" in lower or "permission denied" in lower:
        hints.append("当前用户对源文件或 Excel 自动化没有写权限，请确认文件未被只读或受保护")
    if "rpc server is unavailable" in lower:
        hints.append("Excel 进程未正常启动，或 COM 通道不可用，请关闭残留 Excel 进程后重试")
    if "read-only" in lower or "只读" in lower:
        target = workbook_path.name if workbook_path else "目标工作簿"
        hints.append(f"请关闭占用 {target} 的 Excel 窗口，确认文件未被只读打开后重试")

    prefix = f"{action}失败"
    if workbook_path is not None:
        prefix = f"{action}失败：{workbook_path.name}"
    if hints:
        return f"{prefix} / {raw}。处理建议：{'；'.join(hints)}。"
    return f"{prefix} / {raw}"


def probe_excel_annotation_environment() -> ExcelComProbeResult:
    try:
        pythoncom, win32_client = _load_com_modules()
    except Exception as e:
        return ExcelComProbeResult(ok=False, message=_friendly_excel_error_text("加载 Excel COM 依赖", e))

    excel = None
    _coinitialize(pythoncom)
    try:
        excel = win32_client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        return ExcelComProbeResult(
            ok=True,
            message="Excel COM 环境检查通过。",
            details=(
                "已成功创建 Excel.Application 自动化实例。",
                "源文件标注仍需确保目标工作簿未被 Excel 只读占用。",
            ),
        )
    except Exception as e:
        return ExcelComProbeResult(ok=False, message=_friendly_excel_error_text("创建 Excel.Application", e))
    finally:
        if excel is not None:
            try:
                excel.Quit()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _find_list_object(ws: object, table_name: str) -> object:
    list_objects = ws.ListObjects
    for idx in range(1, int(list_objects.Count) + 1):
        lo = list_objects.Item(idx)
        if str(getattr(lo, "Name", "")).strip() == table_name or str(getattr(lo, "DisplayName", "")).strip() == table_name:
            return lo
    raise RuntimeError(f"未找到目标查询表：{table_name}")


def _column_letter(column_index: int) -> str:
    n = int(column_index)
    chars: list[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(65 + rem))
    return "".join(reversed(chars))


def _normalize_range_values(values: object) -> tuple[tuple[object, ...], ...]:
    if values is None:
        return tuple()
    if isinstance(values, tuple):
        if not values:
            return tuple()
        if isinstance(values[0], tuple):
            return tuple(tuple(row) for row in values)
        return tuple((item,) for item in values)
    return ((values,),)


def _read_nonempty_rows_in_column(ws: object, row_start: int, row_end: int, col: int) -> set[int]:
    if row_end < row_start:
        return set()
    rng = ws.Range(ws.Cells(row_start, col), ws.Cells(row_end, col))
    values = _normalize_range_values(rng.Value)
    rows: set[int] = set()
    for offset, row_values in enumerate(values):
        value = row_values[0] if row_values else None
        if str(value or "").strip():
            rows.add(int(row_start + offset))
    return rows


def _group_consecutive_rows(rows: set[int]) -> list[tuple[int, int]]:
    ordered = sorted(int(x) for x in rows)
    if not ordered:
        return []
    ranges: list[tuple[int, int]] = []
    start = ordered[0]
    prev = ordered[0]
    for row in ordered[1:]:
        if row == prev + 1:
            prev = row
            continue
        ranges.append((start, prev))
        start = row
        prev = row
    ranges.append((start, prev))
    return ranges


def _iter_chunked_addresses(col: int, rows: set[int], max_areas: int = 16, max_chars: int = 6000) -> list[str]:
    groups = _group_consecutive_rows(rows)
    if not groups:
        return []
    col_letter = _column_letter(col)
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0
    for start, end in groups:
        part = f"{col_letter}{start}:{col_letter}{end}" if start != end else f"{col_letter}{start}"
        next_len = current_len + len(part) + (1 if current_parts else 0)
        if current_parts and (len(current_parts) >= max_areas or next_len > max_chars):
            chunks.append(",".join(current_parts))
            current_parts = [part]
            current_len = len(part)
            continue
        current_parts.append(part)
        current_len = next_len
    if current_parts:
        chunks.append(",".join(current_parts))
    return chunks


def _clear_existing_annotation_columns(ws: object, header_row: int, known_headers: tuple[str, ...]) -> set[int]:
    used = ws.UsedRange
    last_col = int(used.Column + used.Columns.Count - 1)
    last_row = int(used.Row + used.Rows.Count - 1)
    previous_rows: set[int] = set()
    for col in range(1, last_col + 1):
        header = str(ws.Cells(header_row, col).Value or "").strip()
        if header in known_headers:
            previous_rows.update(_read_nonempty_rows_in_column(ws, header_row + 1, last_row, col))
            ws.Range(ws.Cells(header_row, col), ws.Cells(last_row, col)).ClearContents()
    return previous_rows


def _apply_fill_to_address(ws: object, address: str, *, clear: bool, color_value: int) -> None:
    try:
        rng = ws.Range(address)
        if clear:
            rng.Interior.Pattern = XL_PATTERN_NONE
        else:
            rng.Interior.Color = color_value
        return
    except Exception:
        if "," not in address:
            raise
    parts = [part for part in address.split(",") if part]
    if len(parts) <= 1:
        raise
    mid = max(1, len(parts) // 2)
    _apply_fill_to_address(ws, ",".join(parts[:mid]), clear=clear, color_value=color_value)
    _apply_fill_to_address(ws, ",".join(parts[mid:]), clear=clear, color_value=color_value)


def _apply_fill_chunks(ws: object, rows_by_col: dict[int, set[int]], *, clear: bool, color_value: int) -> None:
    if not rows_by_col:
        return
    for col, rows in rows_by_col.items():
        for address in _iter_chunked_addresses(col, rows):
            _apply_fill_to_address(ws, address, clear=clear, color_value=color_value)


def _build_annotation_matrix(last_row: int, start_row: int, plan: QueryTableAnnotationPlan) -> tuple[tuple[object, ...], ...]:
    if last_row < start_row:
        return tuple()
    by_row = {
        start_row + int(item.row_index): (item.issue_text, item.rule_ids, item.reasons)
        for item in plan.row_annotations
    }
    matrix: list[tuple[object, object, object]] = []
    for row in range(start_row, last_row + 1):
        matrix.append(by_row.get(row, (None, None, None)))
    return tuple(matrix)


def _write_plan_to_sheet(plan: QueryTableAnnotationPlan, workbook: object, logger: Logger) -> None:
    ws = workbook.Worksheets(plan.worksheet_name)
    lo = _find_list_object(ws, plan.table_name)
    try:
        query_table = lo.QueryTable
        if query_table is not None:
            query_table.PreserveFormatting = True
            query_table.AdjustColumnWidth = False
    except Exception:
        pass

    header_row = int(lo.HeaderRowRange.Row)
    start_row = header_row + 1
    table_end_col = int(lo.Range.Column + lo.Range.Columns.Count - 1)
    start_col = table_end_col + int(plan.gap_columns) + 1

    row_to_prev_annotation = _clear_existing_annotation_columns(ws, header_row, plan.headers)

    used = ws.UsedRange
    last_row = int(max(used.Row + used.Rows.Count - 1, header_row + 1))
    ws.Range(ws.Cells(header_row, start_col - 1), ws.Cells(last_row, start_col - 1)).ClearContents()
    ws.Range(ws.Cells(header_row, start_col), ws.Cells(header_row, start_col + len(plan.headers) - 1)).Value = (plan.headers,)

    matrix = _build_annotation_matrix(last_row=last_row, start_row=start_row, plan=plan)
    if matrix:
        ws.Range(ws.Cells(start_row, start_col), ws.Cells(last_row, start_col + len(plan.headers) - 1)).Value = matrix

    target_columns: dict[str, int] = {}
    list_columns = lo.ListColumns
    for idx in range(1, int(list_columns.Count) + 1):
        item = list_columns.Item(idx)
        col_name = str(getattr(item, "Name", "")).strip()
        if not col_name:
            continue
        try:
            abs_col = int(item.Range.Column)
        except Exception:
            continue
        target_columns[col_name] = abs_col

    row_to_new_annotation = {start_row + int(item.row_index) for item in plan.row_annotations}
    rows_to_reset = row_to_prev_annotation.union(row_to_new_annotation)
    reset_columns = {col: target_columns[col] for col in plan.possible_highlight_columns if col in target_columns}
    rows_by_reset_col = {abs_col: set(rows_to_reset) for abs_col in reset_columns.values()}
    _apply_fill_chunks(ws, rows_by_reset_col, clear=True, color_value=HIGHLIGHT_COLOR)

    rows_by_highlight_col: dict[int, set[int]] = {}
    for item in plan.cell_highlights:
        abs_col = target_columns.get(item.column_name)
        if abs_col is None:
            continue
        rows_by_highlight_col.setdefault(abs_col, set()).add(start_row + int(item.row_index))
    _apply_fill_chunks(ws, rows_by_highlight_col, clear=False, color_value=HIGHLIGHT_COLOR)

    logger.info(
        f"已写入源文件标注：{Path(str(workbook.FullName)).name} / {plan.worksheet_name} / 行数={len(plan.row_annotations)} / 高亮={len(plan.cell_highlights)}"
    )


def write_source_annotations(bundle: SourceAnnotationBundle, logger: Optional[Logger] = None) -> AnnotationWriteResult:
    log = logger or make_logger()
    pythoncom, win32_client = _load_com_modules()
    _coinitialize(pythoncom)
    try:
        workbook_map: dict[Path, list[QueryTableAnnotationPlan]] = {}
        for plan in bundle.plans:
            workbook_map.setdefault(plan.workbook_path.resolve(), []).append(plan)
        touched: list[str] = []
        for workbook_path, plans in workbook_map.items():
            excel = None
            wb = None
            backup_path: Optional[Path] = None
            old_calculation = None
            started = perf_counter()
            try:
                try:
                    excel = win32_client.DispatchEx("Excel.Application")
                except Exception as e:
                    raise RuntimeError(_friendly_excel_error_text("启动 Excel", e, workbook_path)) from e
                excel.Visible = False
                excel.DisplayAlerts = False
                excel.EnableEvents = False
                excel.ScreenUpdating = False
                try:
                    old_calculation = excel.Calculation
                    excel.Calculation = XL_CALC_MANUAL
                except Exception:
                    old_calculation = None

                log.info(f"打开源文件：{workbook_path.name}")
                # 检查文件是否被占用
                try:
                    ensure_no_open_workbook(workbook_path)
                except ValueError as e:
                    raise RuntimeError(f"源文件可能被占用：{workbook_path.name}。请关闭 Excel 窗口后重试。") from e

                # 备份源文件
                backup_path = backup_file(workbook_path)

                try:
                    wb = excel.Workbooks.Open(
                        str(workbook_path),
                        UpdateLinks=0,
                        ReadOnly=False,
                        IgnoreReadOnlyRecommended=True,
                        Notify=False,
                        AddToMru=False,
                    )
                except Exception as e:
                    raise RuntimeError(_friendly_excel_error_text("打开源文件", e, workbook_path)) from e
                if bool(getattr(wb, "ReadOnly", False)):
                    raise RuntimeError(f"源文件已以只读方式打开：{workbook_path.name}。请关闭占用该文件的 Excel 窗口后重试。")
                write_started = perf_counter()
                for plan in plans:
                    _write_plan_to_sheet(plan, wb, log)
                write_elapsed = perf_counter() - write_started
                save_started = perf_counter()
                try:
                    wb.Save()
                except Exception as e:
                    raise RuntimeError(_friendly_excel_error_text("保存源文件", e, workbook_path)) from e

                # 标注成功，删除备份
                try:
                    backup_path.unlink()
                    backup_path = None
                except Exception as e:
                    log.warn(f"删除备份文件失败：{backup_path.name} / {type(e).__name__}: {e}")

                save_elapsed = perf_counter() - save_started
                touched.append(str(workbook_path))
                log.info(
                    f"源文件已保存：{workbook_path.name} / 写入耗时={write_elapsed:.1f}s / 保存耗时={save_elapsed:.1f}s / 总耗时={perf_counter() - started:.1f}s"
                )
            except Exception as original_error:
                if wb is not None:
                    try:
                        wb.Saved = True
                    except Exception:
                        pass
                    try:
                        wb.Close(SaveChanges=False)
                    except Exception as close_error:
                        log.warn(f"回滚前关闭工作簿失败：{workbook_path.name} / {type(close_error).__name__}: {close_error}")
                    wb = None
                if backup_path is not None:
                    try:
                        restore_from_backup(workbook_path, backup_path=backup_path)
                        log.warn(f"源文件标注失败，已从备份恢复：{workbook_path.name}")
                    except Exception as restore_error:
                        raise RuntimeError(
                            f"源文件标注失败且备份恢复失败：{workbook_path.name} / "
                            f"原始错误={type(original_error).__name__}: {original_error} / "
                            f"恢复错误={type(restore_error).__name__}: {restore_error}"
                        ) from restore_error
                raise
            finally:
                if wb is not None:
                    try:
                        wb.Saved = True
                    except Exception:
                        pass
                    try:
                        wb.Close(SaveChanges=False)
                    except Exception as e:
                        log.warn(f"关闭工作簿时出现可忽略异常：{workbook_path.name} / {type(e).__name__}: {e}")
                if excel is not None:
                    try:
                        if old_calculation is not None:
                            excel.Calculation = old_calculation
                    except Exception:
                        pass
                    try:
                        excel.Quit()
                    except Exception:
                        pass
        if touched:
            return AnnotationWriteResult(ok=True, message=f"源文件标注已完成：{len(touched)} 个工作簿。")
        return AnnotationWriteResult(ok=True, message="未找到需要写入的源文件标注。")
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
