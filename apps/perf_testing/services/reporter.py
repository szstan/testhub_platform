"""压测 HTML 报告生成。

设计取舍：报告是一个「可离线归档、可发给别人」的单文件 HTML。
图表用 CDN 引 ECharts，加载不到时自动降级为纯表格 —— 所有数字本身
都内联在 HTML 里，所以断网环境下报告依然完整可读，只是没有曲线。
"""
import html
import json
import logging
import os

from django.conf import settings
from django.utils import timezone

from .metrics import downsample

logger = logging.getLogger(__name__)

#: 曲线最多渲染的点数，超过则等距降采样，避免浏览器卡死
MAX_CHART_POINTS = 600

ECHARTS_CDN = 'https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js'


def generate_report(execution):
    """生成 HTML 报告，返回相对 MEDIA_ROOT 的路径。"""
    from ..models import PerfMetricSample, PerfRequestStat

    art_rel = execution.artifact_dir or os.path.join(
        'perf-testing', 'executions', str(execution.id))
    art_abs = os.path.join(settings.MEDIA_ROOT, art_rel)
    os.makedirs(art_abs, exist_ok=True)
    report_abs = os.path.join(art_abs, 'report.html')

    samples = list(PerfMetricSample.objects.filter(
        execution=execution).order_by('ts_offset').values(
        'ts_offset', 'active_users', 'tps', 'avg_rt', 'p90_rt', 'p95_rt',
        'p99_rt', 'error_rate', 'cpu_percent', 'memory_mb'))
    samples = downsample(samples, MAX_CHART_POINTS)
    stats = list(PerfRequestStat.objects.filter(execution=execution).order_by('-avg_rt'))

    content = _render(execution, samples, stats)
    with open(report_abs, 'w', encoding='utf-8') as fh:
        fh.write(content)

    return os.path.join(art_rel, 'report.html').replace('\\', '/')


# ---------------------------------------------------------------------- #
def _esc(value):
    return html.escape(str(value if value is not None else ''))


def _render(execution, samples, stats):
    summary = execution.summary or {}
    scenario = execution.scenario
    load = execution.load_snapshot or {}

    sla_badge = {
        'PASSED': ('通过', '#059669', '#d1fae5'),
        'FAILED': ('未通过', '#dc2626', '#fee2e2'),
    }.get(execution.sla_result, ('未评估', '#6b7280', '#f3f4f6'))
    status_text = dict(execution.STATUS_CHOICES).get(execution.status, execution.status)

    model_text = {
        'CONCURRENCY': '固定并发', 'RAMPING': '阶梯加压',
        'RPS': '固定 RPS', 'SPIKE': '尖峰冲击',
    }.get(load.get('model'), load.get('model', '-'))

    chart_data = {
        'x': [s['ts_offset'] for s in samples],
        'tps': [round(s['tps'], 2) for s in samples],
        'users': [s['active_users'] for s in samples],
        'avg': [round(s['avg_rt'], 2) for s in samples],
        'p95': [round(s['p95_rt'], 2) for s in samples],
        'p99': [round(s['p99_rt'], 2) for s in samples],
        'err': [round(s['error_rate'], 2) for s in samples],
        'cpu': [round(s['cpu_percent'], 1) for s in samples],
    }

    cards = [
        ('总请求数', f'{summary.get("total_requests", 0):,}', ''),
        ('成功 / 失败', f'{summary.get("success_requests", 0):,} / {summary.get("failed_requests", 0):,}', ''),
        ('错误率', f'{summary.get("error_rate", 0)}%',
         'bad' if (summary.get('error_rate') or 0) > 1 else 'good'),
        ('平均 TPS', f'{summary.get("tps", 0)}', ''),
        ('峰值 TPS', f'{summary.get("peak_tps", 0)}', ''),
        ('平均响应', f'{summary.get("avg_rt", 0)} ms', ''),
        ('P95 响应', f'{summary.get("p95_rt", 0)} ms', ''),
        ('P99 响应', f'{summary.get("p99_rt", 0)} ms', ''),
        ('最大响应', f'{summary.get("max_rt", 0)} ms', ''),
        ('峰值并发', f'{summary.get("max_concurrency", 0)}', ''),
        ('执行时长', f'{execution.duration or 0} s', ''),
        ('压力机CPU峰值', f'{summary.get("peak_load_gen_cpu", 0)}%',
         'bad' if not summary.get('data_trustworthy', True) else 'good'),
    ]
    cards_html = ''.join(
        f'<div class="card"><div class="card-label">{_esc(label)}</div>'
        f'<div class="card-value {cls}">{_esc(value)}</div></div>'
        for label, value, cls in cards)

    warn_html = ''
    if not summary.get('data_trustworthy', True):
        warn_html += (
            '<div class="alert warn">⚠️ 压测过程中压力机 CPU 峰值达 '
            f'{summary.get("peak_load_gen_cpu")}%，已接近单机瓶颈。'
            '此时实测 TPS 可能受限于压力机而非被测服务，报告数据仅供参考。</div>')
    if summary.get('aborted_by_sla'):
        warn_html += ('<div class="alert warn">⚠️ 本次压测因 SLA 持续超限触发自动熔断而提前结束。</div>')
    if execution.error_message:
        warn_html += f'<div class="alert bad">执行错误：{_esc(execution.error_message[:500])}</div>'

    # SLA 明细
    sla_rows = ''.join(
        f'<tr><td>{_esc(d.get("label"))}</td><td>{_esc(d.get("comparator"))} '
        f'{_esc(d.get("threshold"))}</td><td>{_esc(d.get("actual"))}</td>'
        f'<td class="{"ok" if d.get("passed") else "fail"}">'
        f'{"通过" if d.get("passed") else "未通过"}</td></tr>'
        for d in (execution.sla_detail or []))
    sla_html = (f'<h2>SLA 判定</h2><table><thead><tr><th>指标</th><th>阈值</th>'
                f'<th>实测</th><th>结果</th></tr></thead><tbody>{sla_rows}</tbody></table>'
                ) if sla_rows else ''

    # 接口级统计
    stat_rows = ''.join(
        f'<tr><td>{_esc(s.step_name)}</td><td><span class="method">{_esc(s.method)}</span></td>'
        f'<td class="url" title="{_esc(s.url)}">{_esc(s.url[:70])}</td>'
        f'<td>{s.total:,}</td><td>{s.success:,}</td>'
        f'<td class="{"fail" if s.failed else ""}">{s.failed:,}</td>'
        f'<td class="{"fail" if s.error_rate > 1 else ""}">{s.error_rate}%</td>'
        f'<td>{s.tps}</td><td>{s.avg_rt}</td><td>{s.min_rt}</td><td>{s.max_rt}</td>'
        f'<td>{s.p90_rt}</td><td>{s.p95_rt}</td><td>{s.p99_rt}</td></tr>'
        for s in stats)

    # 错误 TOP
    error_rows = ''.join(
        f'<tr><td>{_esc(e.get("type"))}</td><td>{e.get("count", 0):,}</td>'
        f'<td>{_esc(e.get("sample_step"))}</td><td class="url">{_esc(e.get("message"))}</td></tr>'
        for e in (summary.get('error_top') or []))
    error_html = (f'<h2>错误 TOP</h2><table><thead><tr><th>错误类型</th><th>次数</th>'
                  f'<th>示例步骤</th><th>示例信息</th></tr></thead>'
                  f'<tbody>{error_rows}</tbody></table>') if error_rows else ''

    fmt = '%Y-%m-%d %H:%M:%S'
    start_text = timezone.localtime(execution.start_time).strftime(fmt) if execution.start_time else '-'
    end_text = timezone.localtime(execution.end_time).strftime(fmt) if execution.end_time else '-'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>性能测试报告 - {_esc(scenario.name)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:24px; background:#f5f7fa; color:#1f2937;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:1400px; margin:0 auto; }}
  header {{ background:#fff; border-radius:10px; padding:24px; margin-bottom:16px;
            box-shadow:0 1px 3px rgba(0,0,0,.06); }}
  h1 {{ margin:0 0 6px; font-size:22px; }}
  h2 {{ font-size:16px; margin:24px 0 12px; padding-left:10px; border-left:3px solid #2563eb; }}
  .sub {{ color:#6b7280; font-size:13px; }}
  .badge {{ display:inline-block; padding:4px 12px; border-radius:14px; font-size:13px;
            font-weight:600; color:{sla_badge[1]}; background:{sla_badge[2]}; }}
  .meta {{ display:flex; flex-wrap:wrap; gap:10px 32px; margin-top:14px; font-size:13px; color:#4b5563; }}
  .meta b {{ color:#1f2937; font-weight:600; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
            gap:12px; margin-bottom:16px; }}
  .card {{ background:#fff; border-radius:8px; padding:14px 16px; box-shadow:0 1px 3px rgba(0,0,0,.06); }}
  .card-label {{ font-size:12px; color:#6b7280; margin-bottom:6px; }}
  .card-value {{ font-size:20px; font-weight:600; }}
  .card-value.good {{ color:#059669; }}
  .card-value.bad {{ color:#dc2626; }}
  .panel {{ background:#fff; border-radius:10px; padding:20px; margin-bottom:16px;
            box-shadow:0 1px 3px rgba(0,0,0,.06); }}
  .chart {{ width:100%; height:340px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ padding:9px 10px; text-align:left; border-bottom:1px solid #eef1f5; white-space:nowrap; }}
  th {{ background:#f9fafb; color:#4b5563; font-weight:600; }}
  tbody tr:hover {{ background:#f9fafb; }}
  td.url {{ max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#6b7280; }}
  .method {{ font-size:11px; padding:2px 6px; border-radius:3px; background:#eff6ff; color:#2563eb; font-weight:600; }}
  .ok {{ color:#059669; font-weight:600; }} .fail {{ color:#dc2626; font-weight:600; }}
  .alert {{ padding:12px 16px; border-radius:8px; margin-bottom:12px; font-size:13px; }}
  .alert.warn {{ background:#fffbeb; color:#92400e; border-left:3px solid #f59e0b; }}
  .alert.bad {{ background:#fef2f2; color:#991b1b; border-left:3px solid #dc2626; }}
  .fallback {{ display:none; padding:14px; background:#f9fafb; border-radius:6px;
               color:#6b7280; font-size:13px; text-align:center; }}
  footer {{ text-align:center; color:#9ca3af; font-size:12px; padding:16px 0; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{_esc(scenario.name)} <span class="badge">SLA {sla_badge[0]}</span></h1>
    <div class="sub">{_esc(execution.execution_no)} · {_esc(scenario.project.name)}</div>
    <div class="meta">
      <span>执行状态：<b>{_esc(status_text)}</b></span>
      <span>压力模型：<b>{_esc(model_text)}</b></span>
      <span>压测引擎：<b>{_esc(execution.steps_snapshot and scenario.engine or scenario.engine)}</b></span>
      <span>开始：<b>{_esc(start_text)}</b></span>
      <span>结束：<b>{_esc(end_text)}</b></span>
      <span>执行人：<b>{_esc(execution.executed_by.username if execution.executed_by else '系统')}</b></span>
    </div>
  </header>

  {warn_html}

  <div class="cards">{cards_html}</div>

  <div class="panel">
    <h2 style="margin-top:0">TPS 与并发趋势</h2>
    <div id="c1" class="chart"></div>
    <div id="f1" class="fallback">图表库未能加载（可能处于离线环境），下方表格数据不受影响。</div>
    <h2>响应时间趋势</h2>
    <div id="c2" class="chart"></div>
    <h2>错误率与压力机水位</h2>
    <div id="c3" class="chart"></div>
  </div>

  <div class="panel">
    <h2 style="margin-top:0">接口级统计</h2>
    <div style="overflow-x:auto">
    <table><thead><tr>
      <th>步骤</th><th>方法</th><th>URL</th><th>总数</th><th>成功</th><th>失败</th>
      <th>错误率</th><th>TPS</th><th>平均(ms)</th><th>最小</th><th>最大</th>
      <th>P90</th><th>P95</th><th>P99</th>
    </tr></thead><tbody>{stat_rows}</tbody></table>
    </div>
    {error_html}
    {sla_html}
  </div>

  <footer>TestHub 性能测试平台 · 生成于 {timezone.localtime().strftime(fmt)}</footer>
</div>

<script src="{ECHARTS_CDN}"></script>
<script>
var D = {json.dumps(chart_data, ensure_ascii=False)};
(function () {{
  if (typeof echarts === 'undefined') {{
    ['c1','c2','c3'].forEach(function (id) {{
      var el = document.getElementById(id); if (el) el.style.display = 'none';
    }});
    var fb = document.getElementById('f1'); if (fb) fb.style.display = 'block';
    return;
  }}
  var base = {{
    tooltip: {{ trigger: 'axis' }},
    legend: {{ top: 0 }},
    grid: {{ left: 50, right: 50, top: 36, bottom: 30 }},
    xAxis: {{ type: 'category', data: D.x, name: '秒', boundaryGap: false }}
  }};
  function mk(id, series, yAxis) {{
    var el = document.getElementById(id); if (!el) return;
    var chart = echarts.init(el);
    chart.setOption(Object.assign({{}}, base, {{ yAxis: yAxis, series: series }}));
    window.addEventListener('resize', function () {{ chart.resize(); }});
  }}
  mk('c1', [
    {{ name:'TPS', type:'line', smooth:true, showSymbol:false, data:D.tps,
       areaStyle:{{opacity:.12}}, itemStyle:{{color:'#2563eb'}} }},
    {{ name:'并发用户', type:'line', smooth:true, showSymbol:false, yAxisIndex:1,
       data:D.users, itemStyle:{{color:'#f59e0b'}} }}
  ], [{{ type:'value', name:'TPS' }}, {{ type:'value', name:'并发' }}]);
  mk('c2', [
    {{ name:'平均', type:'line', smooth:true, showSymbol:false, data:D.avg, itemStyle:{{color:'#10b981'}} }},
    {{ name:'P95', type:'line', smooth:true, showSymbol:false, data:D.p95, itemStyle:{{color:'#8b5cf6'}} }},
    {{ name:'P99', type:'line', smooth:true, showSymbol:false, data:D.p99, itemStyle:{{color:'#ef4444'}} }}
  ], [{{ type:'value', name:'ms' }}]);
  mk('c3', [
    {{ name:'错误率(%)', type:'line', smooth:true, showSymbol:false, data:D.err,
       itemStyle:{{color:'#dc2626'}}, areaStyle:{{opacity:.12}} }},
    {{ name:'压力机CPU(%)', type:'line', smooth:true, showSymbol:false, yAxisIndex:1,
       data:D.cpu, itemStyle:{{color:'#6366f1'}} }}
  ], [{{ type:'value', name:'错误率' }}, {{ type:'value', name:'CPU', max:100 }}]);
}})();
</script>
</body>
</html>"""
