# -*- coding: utf-8 -*-
"""
多执行对照快照服务：
- build_snapshot：构建指标矩阵快照（与原 GET compare 响应结构一致），供 API 与持久化报告共用
- trim_snapshot_for_ai：压缩快照为文本矩阵喂给 LLM
"""

METRIC_KEYS = ['total_requests', 'tps', 'peak_tps', 'avg_rt', 'p90_rt',
               'p95_rt', 'p99_rt', 'max_rt', 'error_rate']


def _compute_deltas(base_summary, summary):
    deltas = {}
    for key in METRIC_KEYS:
        base_val = base_summary.get(key)
        cur_val = summary.get(key)
        if isinstance(base_val, (int, float)) and base_val and \
                isinstance(cur_val, (int, float)):
            deltas[key] = round((cur_val - base_val) / base_val * 100, 2)
        else:
            deltas[key] = None
    return deltas


def build_snapshot(executions, reference_execution_id=None):
    """构建对照快照。

    :param executions: 已按用户顺序排列的 PerfExecution 列表（≥2）
    :param reference_execution_id: 基准执行 id；缺省用第一条
    :return: dict（JSON 可序列化，created_at 已转 ISO 字符串）
    """
    from ..models import PerfMetricSample, PerfRequestStat
    from ..serializers import PerfMetricSampleSerializer
    from .metrics import downsample

    reference = executions[0]
    if reference_execution_id:
        for e in executions:
            if e.id == reference_execution_id:
                reference = e
                break

    base_summary = reference.summary or {}
    items = []
    for execution in executions:
        summary = execution.summary or {}
        samples = PerfMetricSampleSerializer(
            PerfMetricSample.objects.filter(execution=execution).order_by('ts_offset'),
            many=True).data
        items.append({
            'id': execution.id,
            'execution_no': execution.execution_no,
            'scenario_name': execution.scenario.name if execution.scenario else '',
            'status': execution.status,
            'sla_result': execution.sla_result,
            'created_at': execution.created_at.isoformat() if execution.created_at else None,
            'duration': execution.duration,
            'load_snapshot': execution.load_snapshot,
            'is_reference': execution.id == reference.id,
            'summary': {k: summary.get(k) for k in METRIC_KEYS},
            'delta_pct': _compute_deltas(base_summary, summary),
            'samples': downsample(samples, 300),
        })

    # 接口级对比：按步骤名对齐
    step_names = []
    for execution in executions:
        for stat in PerfRequestStat.objects.filter(execution=execution):
            if stat.step_name not in step_names:
                step_names.append(stat.step_name)
    step_rows = []
    for name in step_names:
        row = {'step_name': name, 'values': []}
        for execution in executions:
            stat = PerfRequestStat.objects.filter(
                execution=execution, step_name=name).first()
            row['values'].append({
                'execution_no': execution.execution_no,
                'tps': stat.tps if stat else None,
                'avg_rt': stat.avg_rt if stat else None,
                'p95_rt': stat.p95_rt if stat else None,
                'error_rate': stat.error_rate if stat else None,
            } if stat else {'execution_no': execution.execution_no})
        step_rows.append(row)

    return {
        'baseline_execution_no': reference.execution_no,
        'reference_execution_id': reference.id,
        'metric_keys': METRIC_KEYS,
        'executions': items,
        'step_comparison': step_rows,
    }


def trim_snapshot_for_ai(snapshot, max_chars=2000):
    """压缩快照为文本矩阵：每执行一行核心指标 + 相对基准 Δ%。"""
    fmt = lambda v: f'{v:.2f}' if isinstance(v, (int, float)) else '-'
    lines = [f"基准执行: {snapshot.get('baseline_execution_no')}",
             '执行 | TPS | 峰值TPS | 平均RT | P95 | P99 | 错误率% | ΔTPS% | ΔP95% | Δ错误率']
    for item in snapshot.get('executions', []):
        s = item.get('summary') or {}
        d = item.get('delta_pct') or {}
        lines.append(' | '.join([
            str(item.get('execution_no', '')),
            fmt(s.get('tps')), fmt(s.get('peak_tps')), fmt(s.get('avg_rt')),
            fmt(s.get('p95_rt')), fmt(s.get('p99_rt')), fmt(s.get('error_rate')),
            fmt(d.get('tps')), fmt(d.get('p95_rt')), fmt(d.get('error_rate')),
        ]))

    step_rows = snapshot.get('step_comparison') or []
    if step_rows:
        lines.append('')
        lines.append('接口级对比（步骤 | 各执行 TPS/P95/错误率%）:')
        for row in step_rows[:20]:
            vals = ['{}/{}/{}'.format(
                fmt(v.get('tps')), fmt(v.get('p95_rt')), fmt(v.get('error_rate')))
                for v in row.get('values', [])]
            lines.append(f"{row.get('step_name')}: " + '; '.join(vals))

    text = '\n'.join(lines)
    return text[:max_chars]
