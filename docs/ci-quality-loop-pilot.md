# CI 质量闭环试点 Runbook

这份 Runbook 用于 TestHub 第一个真实项目的 1～2 周 CI 试点。试点目标是验证结果关联、运行对比、发布门禁和失败反馈是否能进入日常研发流程。

## 试点前准备

需要项目负责人提供：

- TestHub 项目 ID；
- TestHub staging API 地址；
- 一个具有项目权限的 JWT，保存为 CI secret；
- 测试仓库的 `external_ref` 规范；
- JUnit XML 或 Allure JSON 产物路径；
- 当前版本或上一次成功运行的 baseline job ID；
- 是否允许失败、未匹配和 flaky 的门禁阈值。

Token 只能放在 GitHub Actions Secrets、GitLab CI Variables 或 Jenkins Credentials 中。不要写入 YAML、仓库文件、日志或 TestHub 测试结果。

## 推荐 external_ref 规则

优先使用稳定、可读、项目内唯一的标识：

```text
checkout.login
checkout.payment.card
user.profile.update
```

测试标题可以修改，`external_ref` 应长期保持不变。结果中的 JUnit property 或 Allure `testCaseId` 应使用同一个值。

## 长期 CI token

普通 JWT access token 有效期有限，不适合作为长期 GitHub Secret。建议在 staging 上使用项目级 CI token：

```bash
cd /opt/testhub_platform
source venv-prod/bin/activate
python manage.py create_ci_token \
  --project-id 1 \
  --username staging_admin \
  --name github-pilot \
  --expires-days 90
```

命令只输出一次明文 token；不要把输出写入仓库、`.env`、日志或聊天。将输出直接保存为 GitHub Repository Secret `TESTHUB_TOKEN`。Token 只保存哈希，绑定项目，支持过期和撤销；CI 请求使用：

```text
Authorization: Bearer th_ci_...
```

查看 token 元数据（不会显示明文或哈希）：

```bash
python manage.py list_ci_tokens --project-id 1
python manage.py list_ci_tokens --project-id 1 --all
```

撤销 token：

```bash
python manage.py revoke_ci_token --project-id 1 --name github-pilot
```


仓库已经包含可手动触发的 `.github/workflows/testhub-quality-loop.yml`。它要求 self-hosted runner 标签为 `self-hosted, linux, x64, testhub-staging`，并使用以下仓库配置：

- Repository Variable：`TESTHUB_BASE_URL`；
- Repository Secret：`TESTHUB_TOKEN`；
- 手动运行时输入：`project_id`、可选 `baseline_job_id`、`max_unmatched` 和测试命令；初始 pilot 默认允许 unmatched，完成 TestHub 用例物化后将 `max_unmatched` 设为 `0`。

该 workflow 默认不会随每次 push 自动运行，先用 `workflow_dispatch` 做试点，确认关联率和门禁规则后再接入 push/pull_request。
## GitHub Actions

如果暂时不使用仓库内的手动 workflow，也可以把以下步骤嵌入现有 workflow。配置项如下：

- `TESTHUB_BASE_URL`：例如 `https://testhub.example.com`；
- `TESTHUB_TOKEN`：项目级 `th_ci_...` service token；
- workflow_dispatch 输入 `project_id`：TestHub 项目 ID；
- `TESTHUB_BASELINE_JOB_ID`：可选的基线 job ID。

最小流程：

```yaml
name: TestHub quality loop

on:
  push:
    branches: [main]
  pull_request:

jobs:
  quality-loop:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run tests
        run: |
          pytest -q --junitxml=test-results.xml

      - name: Import results into TestHub
        id: import
        env:
          TESTHUB_TOKEN: ${{ secrets.TESTHUB_TOKEN }}
          TESTHUB_BASE_URL: ${{ vars.TESTHUB_BASE_URL }}
          TESTHUB_PROJECT_ID: ${{ vars.TESTHUB_PROJECT_ID }}
        run: |
          python scripts/testhub_ci.py import-results \
            --base-url "$TESTHUB_BASE_URL" \
            --project-id "$TESTHUB_PROJECT_ID" \
            --format junit \
            --external-run-id "github-${{ github.run_id }}" \
            --build-reference "${{ github.sha }}" \
            --file test-results.xml > testhub-import.json
          cat testhub-import.json
          echo "job_id=$(python -c 'import json; print(json.load(open("testhub-import.json"))["job_id"])')" >> "$GITHUB_OUTPUT"

      - name: Evaluate release gate
        env:
          TESTHUB_TOKEN: ${{ secrets.TESTHUB_TOKEN }}
          TESTHUB_BASE_URL: ${{ vars.TESTHUB_BASE_URL }}
          BASELINE_JOB_ID: ${{ vars.TESTHUB_BASELINE_JOB_ID }}
        run: |
          python scripts/testhub_ci.py release-gate \
            --base-url "$TESTHUB_BASE_URL" \
            --job-id "${{ steps.import.outputs.job_id }}" \
            --baseline-job-id "$BASELINE_JOB_ID" \
            --max-failed 0 \
            --max-unmatched 0
```

Pull requests should initially run the gate in report-only mode or use a dedicated staging threshold. Make the gate blocking only after the team has reviewed false positives for several runs.

## GitLab CI

配置 protected/masked CI variables：

- `TESTHUB_BASE_URL`；
- `TESTHUB_TOKEN`；
- `TESTHUB_PROJECT_ID`；
- `TESTHUB_BASELINE_JOB_ID`。

```yaml
stages:
  - test
  - quality

testhub-results:
  stage: test
  image: python:3.12
  script:
    - pytest -q --junitxml=test-results.xml
    - |
      python scripts/testhub_ci.py import-results \
        --base-url "$TESTHUB_BASE_URL" \
        --project-id "$TESTHUB_PROJECT_ID" \
        --format junit \
        --external-run-id "gitlab-$CI_PIPELINE_ID" \
        --build-reference "$CI_COMMIT_SHA" \
        --file test-results.xml > testhub-import.json
  artifacts:
    when: always
    paths:
      - test-results.xml
      - testhub-import.json

quality-gate:
  stage: quality
  image: python:3.12
  needs: [testhub-results]
  script:
    - |
      python scripts/testhub_ci.py release-gate \
        --base-url "$TESTHUB_BASE_URL" \
        --job-id "$(python -c 'import json; print(json.load(open("testhub-import.json"))["job_id"])')" \
        --baseline-job-id "$TESTHUB_BASELINE_JOB_ID" \
        --max-failed 0 \
        --max-unmatched 0
```

## 试点观察指标

每天记录：

- CI 自动关联率；
- unmatched 结果数量和比例；
- 重复导入次数；
- gate 通过/失败次数；
- gate 误阻断次数；
- 平均结果导入时间；
- flaky 用例数量；
- 失败分类分布；
- 从失败到缺陷草稿的时间。

建议试点目标：

- 自动关联率 ≥ 95%；
- unmatched ≤ 5%；
- 重复导入不产生重复 job/step；
- gate 失败有明确 case、signature 和 artifact；
- 试点结束前至少完成一次真实发布门禁。

## 试点结束决策

试点结束后只做三种决策：

1. **扩大使用**：指标达标，接入更多项目；
2. **调整规则**：关联率或门禁误报不达标，先修规则；
3. **暂停功能**：团队不使用或无法提供稳定结果，保留 API 但暂停扩展。

APP authenticated smoke 和 TLS/WSS 不属于本 Runbook 的默认步骤，必须按照 `docs/staging-release-gates.md` 单独授权和执行。
