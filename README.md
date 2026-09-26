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

- `admin` / `123456`（超级用户，视作主管）
- `worker` / `123456`（值守工，可登记脂检）
- `supervisor` / `123456`（主管，可作废脂检）

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
5. **ImpurityTest（杂质抽检 / 脂检）**：归属来脂批、`sampledOn`（抽检日）、`impurityPct`（0～100）、`passed`、`chemist`，以及登记人/作废人与作废时间

**业务规则**：

- 将灶台相位切到 `drawing`（出胶）时，进行中的 CookRun 必须至少有一条 SoftPointProbe 的 `softPointC ≤ 95`。逻辑在 `apps/kiln/services/floor_rules.py`，由相位切换入口调用。
- **新开值守挂某来脂批之前，该批须先有一张仍有效的脂检**：以该批最新一张未作废脂检为准，须「通过」且抽检日不早于开灶日的**五个自然日前**（即 `抽检日 ≥ 开灶日 − 5 天`，相隔恰为 5 天仍有效）。不满足时开灶入口以中文拒绝；开灶入口与来脂批卡片的「有效脂检」标识共用同一判定（`apps/kiln/services/assays.py` 的 `valid_open_assay / assert_can_open_with_lot`），绕过抽检开灶即失败。
- 同批同一自然日只保留一张未作废脂检（DB 部分唯一约束兜底）；撞日拒绝并回显已有检编号。
- **值守工**可登记脂检，**主管**可作废；作废检不再计入有效检。角色用 Django 组「值守工 / 主管」实现（`services/roles.py`）。

## 界面

- 首页：**灶台值守看板** — 左侧班次条（灶 / 脂 / 检）+ 按过道排布的灶台瓦片；点瓦片打开右侧抽屉（值守、探针时间线、改相位 / 登记探针 / 开灶）
- 次页：**来脂批** — 卡片时间线，非宽表 CRUD；卡片标注是否有有效脂检
- **杂质抽检（脂检）**：登记抽检（值守工）、主管作废；页脚说明五日有效窗

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
