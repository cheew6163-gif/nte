# 本次验证记录（2026-10-23）

## 版本依据

- 最新 MD 与用户本轮确认优先；PDF 仅提供首张地图缩放基准及场景参考。
- 复用现有 `E:\AI Agent\copy\nte-publish`，起点 `4a6e31163014f1614efaf456f3f6a2aa046be8af`，检查时本地与远端 main 一致、工作区干净。
- 分支：`codex/campfire-context-20261023`。没有额外克隆、工作树、备份或文件删除。
- 已逐文件比较旧修改包和其解压目录，两者相同；未整包覆盖，也未采用旧文字定位。

## 环境和结果

Windows / Python 3.13.7。仓库内 `.venv` 安装 `requirements.txt`：NumPy 2.2.6、OpenCV 4.11.0.86、MSS 10.1.0、Pillow 11.3.0、DXcam 0.3.0。

```powershell
.venv\Scripts\python.exe -X utf8 -B tools/run_checks.py
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -X utf8 app.py --check-image tests/frames999/map_reference.png --task nte999
.venv\Scripts\python.exe -X utf8 -B tools/map_debug.py --output .validation/map-target-debug.png
git diff --check
```

最终全套：**157 项，0 失败，0 错误**。依赖一致性检查通过，28 个 Python 源文件在内存编译检查通过，Git 空白检查通过。运行器将测试证据留在 `.validation/`，不自动删除；该目录及 `.venv/` 不提交。不是沿用历史测试数量。

初次全套有 1 项错误圈测试失败：其他篝火外围的选中圈仍进入了地形模板。已扩大所有相关图标的遮罩范围；该项及最终全套复测通过。之后补上“已有选中不能算新点击响应”检查，得到上述最终 157 项结果。

## 地图与防误传送

- 独立真值：用户最新截图白圈内篝火，通过西侧长厅、南侧双八角塔和层叠弯道对应到首图，人工标定 `(746,424)`；见 `tests/frames999/target_calibration.json`。测试答案不从算法输出产生。
- 上下文为首图 `[560,290,920,650]` 道路与建筑，排除玩家、图标、选中圈及文字。全局阈值 0.8、各分区最低 0.75、远处次候选差至少 0.08。
- 基准识别 `(746.0,424.0)`，最佳约 1.000，次候选约 0.294，分区最低约 1.000。白圈仅作目标独立定位后的核验。
- 覆盖基准缩放平移、4K 归一化、JPEG、亮度、BGR/BGRA/灰度，移动箭头、去掉文字和图标、错误选中圈、旧面板、重复地形、全局与局部遮挡、点击前目标变化。
- 仅验证首图缩放。旧补充图和新目标确认图约为不同缩放，本次均明确拒绝；不代表任意缩放支持。
- 每轮先重新点击目标；失去地形或地图平移后丢弃原选择。传送要求本轮点击、独立目标一致、该点白圈及传送按钮；定位前和按下前都按 4 个实际像素检查地图锚点。

## 回归与隐私

- 走位与战斗的 `commit/tick/blind/phase_text/check_timeout` 方法 AST 与原版相同。W 3 秒、A 1.8 秒、S 2 秒后松开；之后 80 秒内 40 次左 Shift，战斗期间不截图。
- `config.json`、`core.py`、`settings.py`、`windows.py`、咔呲模板配置和依赖要求均保留原内容。咔呲本地 `battle_watch=14.0` 继续保留，未恢复旧默认值。
- 回归涵盖原咔呲两条结算路径、停止/前台/异常释键、多屏原点、动作前画面校验。
- 6 张公开测试图及新增地图图像已脱敏。移除账号识别配置和分支；`hud.png` 原路径更新为 1×1 黑色占位图，没有个人像素，不再参与识别。
- 日志、traceback、JSON 和截图落盘前脱敏；原始内存帧不变。保护限于已知界面区域、带标签文本及结构化账号字段，不声称任意未知个人内容均能识别。
- 环境 inspect 只存结构化报告，游戏截图只取目标客户区，桌面单步只取显式靶窗，无整桌面回退。

## 实机尝试与未验证事项

已运行只读环境检查。随后执行：

```powershell
.venv\Scripts\python.exe -X utf8 app.py --input-check game --target bonfire --move-only --timeout 8 --start-delay 1 --report-dir .validation/game-step
```

当次 DXGI 初始化报拒绝访问，正常回退 MSS。8 秒内未稳定识别地图（最新分类为人物界面），退出失败，**没有发送鼠标输入**。此结果不是定位或实际传送验收成功。

仍需在首图缩放地图上完成单次真实点击、正确选中和实际传送，然后按 MD 验收完整循环、左 Ctrl 停止及异常释键。新 `--target bonfire` 单步只选择目标，不自动传送；已有正确选中时会独立记录，不能当作本次点击响应。

## GitHub 写入状态

- 当前 GitHub 连接账号与仓库正确，读取成功；账号角色显示可 push，但创建本次工作分支实测仍为 `403 Resource not accessible by integration`。
- CLI `gh auth status` 报认证不可用。本环境原生 Git 凭据助手受 MSYS 对象访问限制；直接调用 GCM 读取凭据也返回 `[0x5] 拒绝访问`。不能据此证明用户交互终端里的凭据无效。
- 本次自动执行尚未成功推送；本地提交保留在上述分支，提交 SHA 以 `git rev-parse HEAD` 为准。需在用户自己的交互终端完成认证/推送，再核对远端 SHA。
- 没有强制推送、改写历史或合并 main。
