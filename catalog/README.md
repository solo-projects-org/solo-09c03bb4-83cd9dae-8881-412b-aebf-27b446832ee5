# Satellite Scene Catalog API

目录服务：遥感数据员登记卫星景的 **B04（红光）/ B08（近红外）** 两个波段文件，
农情分析组按 **地块经纬矩形 + UTC 时段** 查询现行景，并下载已验真的波段文件。

技术栈：FastAPI + SQLite（WAL）+ 本地内容寻址文件存储，Docker Compose 一键启动，
自带 Swagger 入口。

## 1. 启动

```bash
cd catalog
docker compose up --build -d
```

- Swagger UI：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>
- 数据持久化在命名卷 `catalog-data`（SQLite 库 + 波段文件，容器重启不丢）

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/scenes/{scene_id}/revisions` | multipart 表单提交一个修订（元数据 + B04/B08 两文件 + 各自 SHA-256） |
| GET | `/scenes` | 按 bbox（`west,south,east,north`）与 `start_time/end_time`（UTC ISO-8601）查询，只返回每个景的**现行修订** |
| GET | `/scenes/{scene_id}/revisions/{revision}` | 取任意历史修订（含被取代的旧修订）及其下载地址 |
| GET | `/scenes/{scene_id}/revisions/{revision}/bands/{band}/download` | 下载 B04/B08，响应头带 `X-SHA-256` |
| GET | `/scenes/{scene_id}/ndvi?threshold=0.3` | 对**现行修订**的 B04/B08 单景计算 NDVI 统计（阈值 `-1～1`，必填） |
| GET | `/health` | 存活/数据库检查 |

### 提交示例

```bash
curl -X POST http://localhost:8000/scenes/S2A_MSIL1C_001/revisions \
  -F "revision=1" \
  -F "acquisition_time=2026-06-01T10:00:00Z" \
  -F "west=116.0" -F "south=39.0" -F "east=117.0" -F "north=40.0" \
  -F "sha256_b04=$(sha256sum b04.tif | cut -d' ' -f1)" \
  -F "sha256_b08=$(sha256sum b08.tif | cut -d' ' -f1)" \
  -F "file_b04=@b04.tif" \
  -F "file_b08=@b08.tif"
```

### 查询示例

```bash
curl "http://localhost:8000/scenes?west=116.2&south=39.2&east=116.8&north=39.8&start_time=2026-06-01T00:00:00Z&end_time=2026-06-30T23:59:59Z"
```

### 单景 NDVI 示例

```bash
curl "http://localhost:8000/scenes/S2A_MSIL1C_001/ndvi?threshold=0.3"
```

```json
{
  "scene_id": "S2A_MSIL1C_001",
  "revision": 3,
  "acquisition_time": "2026-07-01T10:00:00.000000Z",
  "threshold": 0.3,
  "valid_pixel_count": 1048576,
  "ndvi_mean": 0.4312,
  "fraction_below_threshold": 0.1875
}
```

无有效像元时 `valid_pixel_count=0`，`ndvi_mean` 与 `fraction_below_threshold` 均为 `null`。
该景不存在任何修订返回 `404`；阈值超出 `[-1, 1]`、`NaN`/`Inf` 或缺少该参数返回 `422`。

## 3. 一致性规则（如何满足需求）

1. **双波段验真才可查/可下**：上传先流式落临时盘并实时计算 SHA-256，
   与申报值逐字节比对；任一不符返回 `400` 且不写任何目录记录。只有两份文件
   都通过后才在同一事务里写入 scene + 两条 band 记录。
2. **同修订重复提交幂等**：`(scene_id, revision)` 为主键。内容完全一致的重复提交
   返回 `200` + `submission_status=idempotent_replay`，响应与首次完全相同；
   内容（元数据或任一摘要）冲突返回 `409`，原记录不受影响。
3. **迟到旧修订不能盖新版**：提交时校验 `revision > 该景当前最大修订号`，
   否则 `409 stale revision`。修订号允许跳号。
4. **新版改变时空范围后查询只返回相交的现行景**：`GET /scenes` 用
   `MAX(revision)` 关联每个景的现行修订，再做矩形相交
   （`west<=east and east>=west ...`）与 UTC 时间窗过滤；旧修订不会回流。
5. **已开始的旧波段下载完整可用**：波段文件按 SHA-256 内容寻址、不可变、
   永不删除，旧修订的下载地址长期有效，新修订发布不影响进行中的下载。
6. **校验失败 / 并发 / 重启不会让目录指向缺失文件**：
   - 文件先 `os.replace` 原子落位，数据库行后插入；失败时临时文件立即清理；
   - 写事务使用 `BEGIN IMMEDIATE` + WAL + `busy_timeout`，并发提交被序列化，
     相同内容并发提交只有一个 `201`，其余幂等 `200`，冲突内容一个成功一个 `409`；
   - 启动时执行目录完整性检查（每条 band 记录的文件必须存在），缺失则拒绝启动；
     同时清理崩溃遗留的 `tmp/upload-*.part`。
7. **NDVI 只做分析，不改变入库规则**：目录允许登记任意普通文件（上传规则不变）。
   `GET /scenes/{scene_id}/ndvi` 只针对现行修订，按文件自身块结构对齐的窗口
   （约 512×512 像元、单窗 ≤ 2 MiB 浮点缓冲）分块读取两个单波段 GeoTIFF，
   峰值内存与景大小无关（GDAL 块缓存默认压到 16 MB，可用 `GDAL_CACHEMAX` 覆盖）；
   两个波段必须同格网（宽高、CRS、仿射变换一致），否则只让这一次分析返回 `422`，
   查询与下载照常。各波段按自身 `scale`/`offset`（GDAL 元数据优先，其次同名
   band 标签）换算反射率，未标注则用原值。排除 NoData、非有限值、负反射率与
   零分母的像元后统计有效像元数、NDVI 均值与严格低于阈值的比例。
8. **分析期间可继续提交修订，但不交付过期结果**：分析开始前记录现行修订号，
   结果返回前再次核对；若期间出现了更新的修订，接口返回 `409` 并提示重试，
   绝不把旧修订的统计当作现行结果返回。查询与提交互不阻塞。

## 4. 本地测试（无 Docker 时）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pytest            # 33 个端到端用例（含并发、重启、下载交错、NDVI 分块/缺测/修订交错）
uvicorn app.main:app --port 8000
```

测试覆盖：双波段验真、校验失败无残留、幂等回放、冲突拒绝、迟到修订拒绝、
时空过滤与现行修订、新旧修订下载交错、4 路并发相同/冲突提交、重启持久化、
缺失文件拒绝启动、临时文件启动清理；NDVI 的 scale/offset 换算、未标注原值、
NoData/非有限值/负反射率/零分母排除、无有效像元返回 null、阈值边界、非 GeoTIFF
与格网不符仅拒绝本次分析、分块读取不整景载入、分析期间提交修订导致结果过期。
