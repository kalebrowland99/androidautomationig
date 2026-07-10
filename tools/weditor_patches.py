"""Apply English UI patches to the installed weditor package."""

from __future__ import annotations

from pathlib import Path

PATCH_MARKER_HTML = "<!-- device-lab-english -->"
PATCH_MARKER_JS = "// device-lab-english-patched"


def _weditor_root() -> Path:
    import weditor

    return Path(weditor.__file__).resolve().parent


def _replace_once(content: str, old: str, new: str) -> str:
    if old not in content:
        return content
    return content.replace(old, new, 1)


def _patch_file(path: Path, marker: str, replacements: list[tuple[str, str]]) -> None:
    content = path.read_text(encoding="utf-8")
    for old, new in replacements:
        content = content.replace(old, new)
    if marker not in content:
        if path.suffix == ".html":
            content = content.replace("</head>", f"  {marker}\n</head>", 1)
        else:
            content = f"{marker}\n{content}"
    path.write_text(content, encoding="utf-8")


HTML_REPLACEMENTS = [
    (
        '<el-switch v-model="liveScreen" active-text="实时" inactive-text="静态">',
        '<el-switch v-model="liveScreen" active-text="Live" inactive-text="Static">',
    ),
    (
        '<span style="color: red">因原有的weditor修改难度过大，所以我重新写了一个新工具<a target="_blank" href="https://uiauto.dev">https://uiauto.dev</a>，欢迎试用</span>',
        "",
    ),
    (
        'placeholder="选择或输入"',
        'placeholder="Select or type"',
    ),
    (
        'aria-expanded="false">相关文档 <span class="caret"></span></a>',
        'aria-expanded="false">Documentation <span class="caret"></span></a>',
    ),
    (
        "uiautomator2快速参考",
        "uiautomator2 Quick Reference",
    ),
    (
        "                      坐标\n",
        "                      Coordinates\n",
    ),
    (
        'title="双击复制"',
        'title="Double-click to copy"',
    ),
    (
        'doPositionTap(cursorValue.x, cursorValue.y)">点击</a>',
        'doPositionTap(cursorValue.x, cursorValue.y)">Tap</a>',
    ),
    (
        "copyToClipboard('XCUIElementType' + elem._type)\">复制</small>",
        "copyToClipboard('XCUIElementType' + elem._type)\">Copy</small>",
    ),
    (
        "            代码\n",
        "            Code\n",
    ),
    (
        '<input v-model="autoCopy" type="checkbox"> 自动复制代码',
        '<input v-model="autoCopy" type="checkbox"> Auto-copy code',
    ),
    (
        '<input v-model="useXPathOnly" type="checkbox"> 强制使用XPath',
        '<input v-model="useXPathOnly" type="checkbox"> Force XPath',
    ),
    (
        '<i class="fa fa-unlink"></i> 点击重连',
        '<i class="fa fa-unlink"></i> Reconnect',
    ),
    (
        "codeRunSelected\" :loading=\"codeRunning\">单行或选中运行",
        "codeRunSelected\" :loading=\"codeRunning\">Run Line or Selection",
    ),
    (
        '@click="clearCode">重置代码',
        '@click="clearCode">Reset Code',
    ),
    (
        'title="重启服务"',
        'title="Restart service"',
    ),
    (
        "<!-- Baidu Analytics -->\n<script>\n  var _hmt = _hmt || [];\n  (function () {\n    var hm = document.createElement(\"script\");\n    hm.src = \"https://hm.baidu.com/hm.js?eefa59dfd5fb29fcc57a8b2437ad5ab1\";\n    var s = document.getElementsByTagName(\"script\")[0];\n    s.parentNode.insertBefore(hm, s);\n  })();\n</script>\n",
        "",
    ),
]


JS_REPLACEMENTS = [
    ("label: \"本地设备\"", 'label: "Local device"'),
    ("this.$message.success('复制成功');", "this.$message.success('Copied');"),
    ('title: "重启内核"', 'title: "Kernel restarted"'),
    ('message: "成功"', 'message: "Success"'),
    ('{ name: "应用安装", value: "d.app_install" }', '{ name: "Install app", value: "d.app_install" }'),
    ('{ name: "启动应用", value: "d.app_start" }', '{ name: "Start app", value: "d.app_start" }'),
    ('{ name: "清空应用", value: "d.app_clear" }', '{ name: "Clear app data", value: "d.app_clear" }'),
    ('{ name: "停止应用", value: "d.app_stop" }', '{ name: "Stop app", value: "d.app_stop" }'),
    ('{ name: "当前应用", value: "d.app_current()" }', '{ name: "Current app", value: "d.app_current()" }'),
    ('{ name: "获取应用信息", value: "d.app_info" }', '{ name: "App info", value: "d.app_info" }'),
    ('{ name: "等待应用运行", value: "d.app_wait" }', '{ name: "Wait for app", value: "d.app_wait" }'),
    ('{ name: "窗口大小", value: "d.window_size()" }', '{ name: "Window size", value: "d.window_size()" }'),
    ('{ name: "截图", value: "d.screenshot()" }', '{ name: "Screenshot", value: "d.screenshot()" }'),
    ('{ name: "推送文件", value: "d.push" }', '{ name: "Push file", value: "d.push" }'),
    ('{ name: "执行shell命令", value: "d.shell" }', '{ name: "Run shell command", value: "d.shell" }'),
    ('{ name: "XPath 点击", value: \'d.xpath("购买").click()\' }', '{ name: "XPath click", value: \'d.xpath("Buy").click()\' }'),
    ('{ name: "剪贴板设置", value: "d.clipboard = " }', '{ name: "Set clipboard", value: "d.clipboard = " }'),
    ('{ name: "剪贴板获取", value: "d.clipboard" }', '{ name: "Get clipboard", value: "d.clipboard" }'),
    ('{ name: "上滑60%", value: \'d.swipe_ext("up", 0.6)\' }', '{ name: "Swipe up 60%", value: \'d.swipe_ext("up", 0.6)\' }'),
    ('{ name: "右滑60%", value: \'d.swipe_ext("right", 0.6)\' }', '{ name: "Swipe right 60%", value: \'d.swipe_ext("right", 0.6)\' }'),
    ('{ name: "显示信息", value: "d.info" }', '{ name: "Device info", value: "d.info" }'),
    ('{ name: "最长等待时间", value: "d.implicitly_wait(20)" }', '{ name: "Implicit wait", value: "d.implicitly_wait(20)" }'),
    ('{ name: "常用设置", value: "d.settings" }', '{ name: "Settings", value: "d.settings" }'),
    ('{ name: "服务最大空闲时间", value: "d.set_new_command_timeout" }', '{ name: "Command timeout", value: "d.set_new_command_timeout" }'),
    ('{ name: "调试开关", value: "d.debug = True" }', '{ name: "Debug mode", value: "d.debug = True" }'),
    ('{ name: "坐标点击 x,y", value: "d.click" }', '{ name: "Click x,y", value: "d.click" }'),
    ('{ name: "获取图层", value: "d.dump_hierarchy()" }', '{ name: "Dump hierarchy", value: "d.dump_hierarchy()" }'),
    ('{ name: "监控", value: "d.watcher" }', '{ name: "Watcher", value: "d.watcher" }'),
    ('{ name: "停止uiautomator", value: "d.uiautomator.stop()" }', '{ name: "Stop uiautomator", value: "d.uiautomator.stop()" }'),
    ('{ name: "视频录制", value: "d.screenrecord(\'output.mp4\')" }', '{ name: "Screen record", value: "d.screenrecord(\'output.mp4\')" }'),
    ('{ name: "停止视频录制", value: "d.screenrecord.stop()" }', '{ name: "Stop screen record", value: "d.screenrecord.stop()" }'),
    ('{ name: "回到桌面", value: \'d.press("home")\' }', '{ name: "Press home", value: \'d.press("home")\' }'),
    ('{ name: "返回", value: \'d.press("back")\' }', '{ name: "Press back", value: \'d.press("back")\' }'),
    ('{ name: "等待activity", value: \'d.wait_activity("xxxx", timeout=10)\' }', '{ name: "Wait for activity", value: \'d.wait_activity("xxxx", timeout=10)\' }'),
    ('{ name: "状态信息", value: "d.status()" }', '{ name: "Status", value: "d.status()" }'),
    ('{ name: "等待就绪", value: "d.wait_ready(timeout=300)" }', '{ name: "Wait until ready", value: "d.wait_ready(timeout=300)" }'),
    ('{ name: "截图保存", value: "d.screenshot().save" }', '{ name: "Save screenshot", value: "d.screenshot().save" }'),
    ('{ name: "截图+旋转+保存", value: \'s.screenshot().transpose(Image.ROTATE_90).save("correct.png")\' }', '{ name: "Screenshot rotate save", value: \'s.screenshot().transpose(Image.ROTATE_90).save("correct.png")\' }'),
    ('{ name: "启动应用设置", value: "d.app_launch(\'com.apple.Preferences\')" }', '{ name: "Launch Settings app", value: "d.app_launch(\'com.apple.Preferences\')" }'),
    ('{ name: "将应用放到前台", value: "d.app_activate" }', '{ name: "Activate app", value: "d.app_activate" }'),
    ('{ name: "杀掉应用", value: "d.app_terminate" }', '{ name: "Terminate app", value: "d.app_terminate" }'),
    ('{ name: "获取应用状态", value: "d.app_state" }', '{ name: "App state", value: "d.app_state" }'),
    ('{ name: "设置搜索等待时间", value: "d.implicitly_wait(30.0)" }', '{ name: "Set implicit wait", value: "d.implicitly_wait(30.0)" }'),
    ('{ name: "窗口UI大小", value: "d.window_size()" }', '{ name: "Window size", value: "d.window_size()" }'),
    ('{ name: "点击", value: "d.click" }', '{ name: "Click", value: "d.click" }'),
    ('{ name: "双击", value: "d.double_tap" }', '{ name: "Double tap", value: "d.double_tap" }'),
    ('{ name: "滑动", value: "d.swipe" }', '{ name: "Swipe", value: "d.swipe" }'),
    ('{ name: "从中央滑动到底部", value: "d.swipe(0.5, 0.5, 0.5, 0.99)" }', '{ name: "Swipe center to bottom", value: "d.swipe(0.5, 0.5, 0.5, 0.99)" }'),
    ('{ name: "长按1s", value: "d.tap_hold(x, y, 1.0)" }', '{ name: "Long press 1s", value: "d.tap_hold(x, y, 1.0)" }'),
    ('{ name: "输入", value: "d.send_keys" }', '{ name: "Send keys", value: "d.send_keys" }'),
    ('{ name: "弹窗点击", value: "d.alert.click(按钮名)" }', '{ name: "Alert click", value: "d.alert.click(button_name)" }'),
    ('{ name: "弹窗按钮", value: "d.alert.buttons()" }', '{ name: "Alert buttons", value: "d.alert.buttons()" }'),
    ('{ name: "等待弹窗", value: "d.alert.wait(timeout=20.0)" }', '{ name: "Wait for alert", value: "d.alert.wait(timeout=20.0)" }'),
    ('{ name: "弹窗是否存在", value: "d.alert.exists" }', '{ name: "Alert exists", value: "d.alert.exists" }'),
    ('{ name: "点击", value: "click()" }', '{ name: "Click", value: "click()" }'),
    ('{ name: "存在时点击", value: "click_exists()" }', '{ name: "Click if exists", value: "click_exists()" }'),
    ('{ name: "等待元素出现", value: "wait()" }', '{ name: "Wait for element", value: "wait()" }'),
    ('{ name: "等待元素消失", value: "wait_gone()" }', '{ name: "Wait until gone", value: "wait_gone()" }'),
    ('{ name: "是否存在", value: "exists" }', '{ name: "Exists", value: "exists" }'),
    ('{ name: "控件截图", value: "screenshot()" }', '{ name: "Element screenshot", value: "screenshot()" }'),
    ('{ name: "控件上滑", value: \'swipe("up")\' }', '{ name: "Swipe up", value: \'swipe("up")\' }'),
    ('{ name: "获取控件中心点坐标", value: "center()" }', '{ name: "Center coordinates", value: "center()" }'),
    ('{ name: "信息", value: "info" }', '{ name: "Info", value: "info" }'),
    ('{ name: "获取Element", value: "get()" }', '{ name: "Get element", value: "get()" }'),
    ('{ name: "返回所有匹配", value: "all()" }', '{ name: "Get all matches", value: "all()" }'),
    ('{ name: "启动应用", value: "d.app_launch" }', '{ name: "Launch app", value: "d.app_launch" }'),
    ("// 初始化变量", "// Initialize variables"),
    ("// 用蓝色的breakpoint标记已经运行过的代码", "// Mark executed lines with breakpoints"),
    ("// 用另外的breakpoint标记当前运行中的代码", "// Mark the current line with another breakpoint"),
    ("// 代码行号:lineno 从0开始", "// Line numbers in lineno start at 0"),
    ('// 下面这两行注释掉，因为会影响 "运行当前行" 功能中的自动跳到下一行的功能', "// Disabled: interferes with auto-advance after run-line"),
    ("// 移动光标", "// Move cursor"),
    ("// 屏幕滚动到当前行", "// Scroll to current line"),
    ("// 显示的名字,没什么乱用", "// Display name"),
    ("// 插入的值", "// Inserted value"),
    ("// 分数越大，排名越靠前", "// Higher score ranks earlier"),
    ("//描述,", "// Description"),
    ("// 如果没有选中，使用光标所在行代码", "// If nothing selected, use the current line"),
    ("// 修正服务端的行号", "// Adjust server-side line number"),
    ("// 运行完后调转到下一行，方便连续点击", "// Jump to next line after run for rapid execution"),
    ("this.editor.selection.moveTo(lineno, 0) // 移动光标", "this.editor.selection.moveTo(lineno, 0) // Move cursor"),
    ("this.pyshell.lineno.current = this.pyshell.lineno.offset // 重置编辑器当前行号", "this.pyshell.lineno.current = this.pyshell.lineno.offset // Reset editor line number"),
]


def revert_weditor_screen_fit() -> None:
    """Restore weditor's original screenshot sizing behavior."""
    root = _weditor_root()
    style_path = root / "static" / "style.css"
    js_path = root / "static" / "js" / "index.js"

    style = style_path.read_text(encoding="utf-8")
    marker = "/* device-lab-screen-fit */"
    if marker in style:
        style = style.split(marker, 1)[0].rstrip() + "\n"
        style = style.replace(
            "#upper {\n  width: 100%;\n  display: flex;\n  flex: 1;\n  border-top: 1px solid black;\n  min-height: 0;\n}",
            "#upper {\n  width: 100%;\n  display: flex;\n  flex: 1;\n  border-top: 1px solid black;\n}",
        )
        style_path.write_text(style, encoding="utf-8")

    js = js_path.read_text(encoding="utf-8")
    if "device-lab-screen-fit-v2" in js or "scheduleScreenResize" in js:
        js = js.replace("self.scheduleScreenResize(img);", "self.resizeScreen(img);")
        js = js.replace(
            """    resizeScreen(img) {
      // device-lab-screen-fit-v2
      var screenDiv = document.getElementById('screen');
      if (!screenDiv) {
        return;
      }
      if (!img) {
        img = this.lastScreenSize && this.lastScreenSize.canvas;
        if (!img || !img.width) {
          return;
        }
      }
      var rect = screenDiv.getBoundingClientRect();
      var pad = 20;
      var availableWidth = Math.max(rect.width - pad * 2, 50);
      var availableHeight = Math.max(rect.height - pad * 2, 50);
      var scale = Math.min(availableWidth / img.width, availableHeight / img.height) * 0.96;
      var displayWidth = Math.floor(img.width * scale);
      var displayHeight = Math.floor(img.height * scale);
      this.lastScreenSize = {
        canvas: { width: img.width, height: img.height },
        screen: { width: availableWidth, height: availableHeight },
        scale: scale,
      };
      Object.assign(this.canvasStyle, {
        width: displayWidth + 'px',
        height: displayHeight + 'px',
      });
    },
    scheduleScreenResize(img) {
      var self = this;
      var delays = [0, 100, 300];
      delays.forEach(function (delay) {
        setTimeout(function () { self.resizeScreen(img); }, delay);
      });
    },""",
            """    resizeScreen(img) {
      // check if need update
      if (img) {
        if (this.lastScreenSize.canvas.width == img.width &&
          this.lastScreenSize.canvas.height == img.height) {
          return;
        }
      } else {
        img = this.lastScreenSize.canvas;
        if (!img) {
          return;
        }
      }
      var screenDiv = document.getElementById('screen');
      this.lastScreenSize = {
        canvas: {
          width: img.width,
          height: img.height
        },
        screen: {
          width: screenDiv.clientWidth,
          height: screenDiv.clientHeight,
        }
      }
      var canvasRatio = img.width / img.height;
      var screenRatio = screenDiv.clientWidth / screenDiv.clientHeight;
      if (canvasRatio > screenRatio) {
        Object.assign(this.canvasStyle, {
          width: Math.floor(screenDiv.clientWidth) + 'px', //'100%',
          height: Math.floor(screenDiv.clientWidth / canvasRatio) + 'px', // 'inherit',
        })
      } else {
        Object.assign(this.canvasStyle, {
          width: Math.floor(screenDiv.clientHeight * canvasRatio) + 'px', //'inherit',
          height: Math.floor(screenDiv.clientHeight) + 'px', //'100%',
        })
      }
    },""",
        )
        js = js.replace(
            """        .always(() => {
          this.dumping = false
          var self = this;
          setTimeout(function () { self.resizeScreen(); }, 0);
          setTimeout(function () { self.resizeScreen(); }, 200);
        })""",
            """        .always(() => {
          this.dumping = false
        })""",
        )
        js_path.write_text(js, encoding="utf-8")


def patch_weditor_english() -> None:
    revert_weditor_screen_fit()
    root = _weditor_root()
    _patch_file(root / "templates" / "index.html", PATCH_MARKER_HTML, HTML_REPLACEMENTS)
    _patch_file(root / "static" / "js" / "index.js", PATCH_MARKER_JS, JS_REPLACEMENTS)
