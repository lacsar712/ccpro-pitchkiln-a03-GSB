# PitchKiln-01 · 灶台值守看板

Django 5 + PostgreSQL：灶台瓦片看板 + 右侧抽屉探针时间线，无 Vue/React SPA。

## 技术栈

- Django 5、PostgreSQL
- Session 登录
- HTMX：局部刷新灶台网格与抽屉
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4710** |
| Postgres | **6110**（容器内 5432） |

数据库账号：`pitchkiln` / `pitchkiln` / 库名 `pitchkiln`

## 快速启动

```bash
cd PitchKiln/PitchKiln-01
docker compose up --build -d
```

浏览器打开：http://localhost:4710

演示账号：

- `admin` / `123456`（超级用户）
- `worker` / `123456`（普通用户）

容器启动时会自动：`migrate` → `seed_data` → `collectstatic` → `gunicorn`

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 确保本机 Postgres 监听 6110，或先 docker compose up -d db
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6110
python manage.py migrate
python manage.py seed_data
python manage.py runserver 0.0.0.0:4710
```

## 业务模型

1. **ResinLot（来脂批）**：`lotCode`、`originPlace`、`arrivalKg`、`receivedAt`
2. **FireHearth（灶台）**：`lane`、`tag`（唯一）、`resinGrade`、相位 `cold|charging|ramping|holding|drawing`
3. **CookRun（熬制值守）**：归属灶台与来脂批、`openedAt`、`closedAt`（可空）、`targetSoftPointC`
4. **SoftPointProbe（软化点探针）**：归属值守、`sampledAt`、`softPointC`、`samplerName`
5. **ImpurityAssay（杂质抽检 / 脂检）**：归属来脂批、`sampledOn`（抽检日）、`impurityPct`（0–100）、`passed`（是否通过）、`chemistName`（化验人）、登记人/作废人与作废时间

**业务规则**：
- 将灶台相位切到 `drawing`（出胶）时，进行中的 CookRun 必须至少有一条 SoftPointProbe 的 `softPointC ≤ 95`。逻辑在 `apps/kiln/services/floor_rules.py`，由相位切换入口调用。
- **开灶须先有有效脂检**：挂来脂批开灶时，该批须存在最新一张**未作废且通过**的 ImpurityAssay，且抽检日不早于**五个自然日**前（含第五日，按自然日不计时分）。任一不满足则中文拒绝。逻辑在 `apps/kiln/services/assay_rules.py`，开灶表单、开灶视图与界面「有效检」标记**同源**调用，绕过表单直开也会失败。
- 同批同一自然日只允许一张未作废抽检：撞日拒绝并回显已有记录编号；数据库有部分唯一约束兜底并发。值守工可登记抽检，主管（staff）可作废，作废检不再计为有效。

## 界面

- 首页：**灶台值守看板** — 左侧班次条（灶 / 脂 / 检）+ 按过道排布的灶台瓦片；点瓦片打开右侧抽屉（值守、探针时间线、改相位 / 登记探针 / 开灶）
- 次页：**来脂批** — 卡片时间线，卡片标注该批当前「有效检 / 无有效检」（与开灶判定同源）
- **脂检台账**（班次条「检」）：登记杂质抽检（值守工可建）；每张卡显示有效检 / 未通过 / 已过五日窗 / 已作废，主管可作废

## 脂检五日有效窗

有效检 = 该批最新一张**未作废、判定通过**的抽检，且 `抽检日 ≥ 开灶日 − 5 天`（自然日）。
例：10 月 26 日开灶，抽检日最早可为 10 月 21 日；10 月 20 日的通过检已超窗，不能开灶。未通过检 / 已作废检 / 超窗检均不满足开灶前置。

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有灶台则只保证账号存在。样例地名仅用「松脂坳 / 桐油坑」系。

## 目录结构

```
PitchKiln-01/
  manage.py
  requirements.txt
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  config/
  apps/kiln/          # 模型、视图、floor_rules、种子
  templates/floor/    # 值守看板 + 抽屉
  templates/resin/    # 来脂批时间线
  static/css/         # 值守台 ops-console 样式
```
