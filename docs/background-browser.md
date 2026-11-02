# 后台浏览器获取

默认获取流程不切换研究者的浏览器窗口，不控制系统鼠标键盘，也不借用研究者正在使用的标签页。开放接口先执行；需要会话权限的下载交给任务自己的后台标签页。研究者可以同时使用其他应用或自己的浏览器页。

`BackgroundBrowserWorker` 是可替换的宿主适配器，当前提供本机 Kimi Browser Extension 的 `WebBridgeTransport`。已有浏览器登录态留在浏览器里，不导出 cookies。扩展需由使用者安装和连接；组件不会自动安装扩展或修改浏览器安全设置。后台仍可能有任务标签页和扩展调试提示，这里的目标是不抢占桌面输入，并不承诺界面上完全不可见。

## 接到现有获取任务

```python
from research_base.browser_worker import BackgroundBrowserWorker, WebBridgeTransport

bridge = WebBridgeTransport("lab-literature-task")
worker = BackgroundBrowserWorker(bridge, background_input=True)
worker.open(official_landing_url, group_title="文献获取")
links = worker.pdf_links()  # 仅导航，不算已取得全文

# 由宿主从当前实际页面的链接中选择目标；不能把引用文献或补充材料当正文。
download = worker.download_pdf(chosen_observed_pdf_url, browser_task["handoff"]["download_dir"])
base.submit_browser_result(
    task_id=browser_task["task_id"],
    lease_token=browser_task["lease"]["token"],
    submission_id=unique_submission_id,
    result={"outcome": "downloaded", "file": {"kind": "local_path", "path": download["path"]},
            "source_url": download["source_url"]},
)
bridge.close()
```

浏览器租约描述带 `execution_policy`，要求 background、禁止 foreground、OS input 和 borrowed user tabs。宿主使用本适配器落实要求；协议声明本身不能阻止任意外部程序另行控制桌面。一个任务会话串行执行浏览器动作，避免多个宿主争用同一个标签页；论文解析和其他任务可以并行。租约有效期与续期仍使用原接口。

该适配器没有激活窗口、系统输入或通用 CDP 执行接口。复用标签页必须得到 bridge 的 `borrowed=false`，否则拒绝；每次操作前重新绑定该任务的目标。研究 agent 不会获得任意浏览器控制工具。

## 需要交互的页面

宿主可使用 `capture_page()` 获取**该标签页**的截图，再把当前 observation_id 和观察到的坐标交给 `click_in_page()`。坐标使用 viewport 的 CSS 像素；设备缩放时要按截图像素与 viewport 尺寸换算。旧截图不能重放输入。

后台输入使用标签页定向的 `Input.dispatchMouseEvent`，在该操作期间启用 `Emulation.setFocusEmulationEnabled`，并在 finally 中恢复。不会调用 `Page.bringToFront`、`Target.activateTarget` 或操作系统输入。这基于 [Chrome DevTools Protocol 的 Emulation 接口](https://chromedevtools.github.io/devtools-protocol/tot/Emulation/)及 [Input 接口](https://chromedevtools.github.io/devtools-protocol/tot/Input/)。后端必须明确支持这一能力；交互送达不代表站点已接受，须重新观察实际页面。不支持或未通过时保留缺口，不自动抢占桌面。

2026-11-03 实测中，Cell 的验证页使用这一后台路径后进入 State 正式开放获取页面，随后取到与正式 DOI 匹配的 PDF。此前 bioRxiv 的一次验证曾使用前台操作；研究者提出不占用桌面后，后续四篇 bioRxiv 下载与 Cell 交互/下载均采用后台路径。不能把这次成功推广为所有站点的无人值守保证。

## 大文件与版本

浏览器内以 100 MiB 为默认上限完整接收 PDF，计算 SHA256；本地按 512 KiB 分块接收，校验连续偏移、字节数、PDF 文件头及整体 SHA256。传输未完成只留下临时文件，失败清理；页面临时缓存结束后删除。有效 PDF 字节随后仍需经过论文、版本及材料角色检查。State 正式 PDF 实测为 27,988,343 字节，54 个分块，浏览器与本地 SHA256 一致。

bioRxiv/medRxiv 的同一 DOI 可有不同修订。组件从官方版本链接保留 revision 标识，不用新版本覆盖旧版本原件；有明确 posted_date 的请求会核对 PDF 的版本日期页脚。只有 DOI 相同不能证明 v1 与 v3 相同。本地文件导入可传 `source_url` 保留实际下载出处；该出处由可信宿主提供，本身不替代文件身份检查。

下载结果中的 policy、字节数和哈希供宿主检查；原始 PDF、截图、下载载荷和浏览器会话数据不进入公开仓库。
