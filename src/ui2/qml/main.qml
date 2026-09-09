import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import QtMultimedia

ApplicationWindow {
    id: window
    // 由 Python 入口在完成图标与深色标题栏设置后显示，避免启动瞬间闪出
    // Windows 默认白色标题栏。
    visible: false
    // 总览首屏包含指标卡、任务队列和趋势图。默认尺寸稍微放大，
    // 让常用信息首次打开时尽量完整呈现，只有小分辨率才需要滚动。
    width: 1600
    height: 1000
    minimumWidth: 1100
    minimumHeight: 720
    property string appVersion: "2.1.1"
    title: "多平台采集工作台 " + appVersion
    color: "#0b1220"

    property color ink: "#f2f7ff"
    property color muted: "#94a7bf"
    property color panel: "#142135"
    property color panel2: "#192a42"
    property color line: "#263952"
    property color blue: "#6ea7ff"
    property color green: "#45d5a1"
    property color amber: "#f0b75a"

    // 统一 Qt Quick Controls 的控件和弹出层调色板；否则 ComboBox 弹层会
    // 回退到系统白底，与工作台深色主题不一致。
    palette.window: "#0d1727"
    palette.base: "#111d30"
    palette.alternateBase: "#17273d"
    palette.button: "#192a42"
    palette.buttonText: "#f2f7ff"
    palette.text: "#f2f7ff"
    palette.highlight: "#31518a"
    palette.highlightedText: "#ffffff"

    // 左上角统一切换工作区。采集页签与发布中心是两个独立工作区，
    // 切换时只改变当前页面，不重新创建整棵界面，避免出现闪烁和卡顿。
    property string workspaceMode: "collect"
    property string lastCollectionPage: "overview"
    // currentPage 的 notify 信号也会在后台状态更新时触发；账号页的
    // BitBrowser 识别只能在真正切换到账号页时执行，不能跟着每次刷新重复执行。
    property string lastAutoInspectedPage: ""
    function showPage(page) {
        var target = String(page || "overview")
        if (target === "publish") {
            workspaceMode = "publish"
            backend.selectPage("publish")
            return
        }
        workspaceMode = "collect"
        lastCollectionPage = target
        backend.selectPage(target)
    }
    function switchWorkspace(mode) {
        var target = String(mode || "collect")
        if (target === "publish") {
            workspaceMode = "publish"
            backend.selectPage("publish")
            return
        }
        workspaceMode = "collect"
        backend.selectPage(lastCollectionPage || "overview")
    }
    function overviewValue(key) {
        var values = JSON.parse(backend.snapshotJson || "{}")
        return values[key] || 0
    }
    function humanWaitingText() {
        var names = overviewValue("waiting_human")
        if (names && names.length !== undefined && names.length > 0)
            return names.join("、")
        return ""
    }
    function overviewPercent(numerator, denominator) {
        if (!denominator || denominator <= 0) return "0%"
        return Math.round((numerator * 1000) / denominator) / 10 + "%"
    }
    function taskTimeLabel(value) {
        var text = String(value || "").replace("T", " ")
        if (!text) return "—"
        return text.length > 16 ? text.slice(0, 16) : text
    }
    function isRunningStatus(status) {
        return status === "phase_a_search" || status === "phase_b_comments" || status === "running"
    }
    function canResumeStatus(status) {
        return ["paused", "incomplete", "aborted", "stopped", "failed", "error", "exception", "interrupted", "crashed", "no_more", "no_account", "waiting_account", "waiting_human"].indexOf(status) >= 0
    }
    function taskNeedsHumanResume(task) {
        var run = task ? (task.latest_run || {}) : {}
        var reason = String(run.stop_reason || "") + " " + String(task ? task.error_reason || "" : "")
        return task && (task.status === "waiting_human" ||
                        reason.indexOf("人工") >= 0 || reason.indexOf("验证") >= 0)
    }
    function taskActionText(task) {
        var status = task ? String(task.status || "") : ""
        if (isRunningStatus(status)) return "暂停"
        if (taskNeedsHumanResume(task)) return "继续"
        if (status === "paused") return "继续"
        if ((status === "incomplete" || status === "no_more") && taskStatusLabel(task) === "暂无更多视频") return "继续采集"
        if (canResumeStatus(status) || status === "pending") return "开始"
        return "查看"
    }
    function isKeywordFinished(item) {
        return item && (item.status === "completed" || item.status === "no_more")
    }
    function taskStatusLabel(task) {
        var run = task ? (task.latest_run || {}) : {}
        var reason = String(run.stop_reason || "") + " " + String(task ? task.error_reason || "" : "")
        var rawStatus = String(task ? task.raw_status || "" : "")
        if (rawStatus === "interrupted" || rawStatus === "crashed")
            return "异常中断"
        if (task && task.status === "incomplete" &&
                (reason.indexOf("没有更多") >= 0 || reason.indexOf("暂无更多") >= 0))
            return "暂无更多视频"
        return task && task.status_label ? task.status_label : "未知状态"
    }
    function canExportTaskStatus(status) {
        // “暂无更多视频”是正常的终止状态：平台结果已经耗尽，
        // 即使没有达到目标数量，也应允许导出已经采集到的内容。
        return ["paused", "stopped", "done", "completed", "failed", "incomplete", "aborted", "no_more"].indexOf(status) >= 0
    }
    function canOpenTaskFolderStatus(status) {
        return canExportTaskStatus(status)
    }

    // 所有操作按钮共用同一套悬停反馈，避免用户无法判断当前控件是否被选中。
    component AppButton: Button {
        id: appButton
        hoverEnabled: true
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        Rectangle {
            anchors.fill: parent
            radius: 7
            color: "transparent"
            border.color: appButton.enabled && appButton.hovered ? window.blue : "transparent"
            border.width: appButton.enabled && appButton.hovered ? 1 : 0
            z: 100
            enabled: false
        }
    }

    // 复选框固定指示器尺寸和文字起点，避免 Basic 样式在中文长文本下错位。
    component AppCheckBox: CheckBox {
        id: appCheckBox
        implicitHeight: 30
        leftPadding: 24
        spacing: 0
        hoverEnabled: true
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        indicator: Rectangle {
            x: 0
            y: (appCheckBox.height - height) / 2
            width: 16
            height: 16
            radius: 3
            color: appCheckBox.checked ? window.blue : "transparent"
            border.color: appCheckBox.checked ? window.blue : (appCheckBox.hovered ? window.blue : "#7187a3")
            Text {
                anchors.centerIn: parent
                text: appCheckBox.checked ? "✓" : ""
                color: "#071224"
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
        }
        contentItem: Text {
            text: appCheckBox.text
            color: appCheckBox.enabled ? window.muted : "#536681"
            verticalAlignment: Text.AlignVCenter
            wrapMode: Text.WordWrap
            elide: Text.ElideRight
            font.pixelSize: 11
        }
    }

    // ComboBox 的输入框和下拉项共用工作台颜色，避免 Basic 样式把选项
    // 渲染成白底黑字。各平台下拉数据统一使用 label/content/字符串显示。
    Component {
        id: darkComboDelegate
        ItemDelegate {
            width: ListView.view ? ListView.view.width : 240
            height: 34
            padding: 0
            highlighted: ListView.isCurrentItem
            contentItem: Text {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 8
                text: {
                    if (modelData && modelData.label !== undefined) return modelData.label
                    if (modelData && modelData.content !== undefined) return modelData.content
                    return modelData
                }
                color: "#f2f7ff"
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                font.pixelSize: 11
            }
            background: Rectangle {
                color: parent.highlighted ? "#31518a" : "transparent"
                border.color: parent.highlighted ? "#4776aa" : "transparent"
                radius: 5
            }
        }
    }

    // 1.2 使用的是蓝色线性图标。2.0 不再依赖系统字体里的 Unicode 字形，
    // 这样缩放、换电脑字体或切换平台时都不会出现锯齿和错图标。
    Component {
        id: lineIconComponent
        Item {
            property string kind: "overview"
            property color tint: "#56b4ff"
            implicitWidth: 24
            implicitHeight: 24
            Canvas {
                id: lineCanvas
                anchors.fill: parent
                antialiasing: true
                onPaint: {
                    var ctx = getContext("2d")
                    ctx.clearRect(0, 0, width, height)
                    ctx.strokeStyle = String(parent.tint)
                    ctx.fillStyle = String(parent.tint)
                    ctx.lineWidth = 1.8
                    ctx.lineCap = "round"
                    ctx.lineJoin = "round"
                    var w = width
                    var h = height
                    var k = parent.kind
                    if (k === "brand") {
                        ctx.beginPath()
                        ctx.moveTo(3, 8); ctx.lineTo(3, 3); ctx.lineTo(8, 3)
                        ctx.moveTo(w - 8, 3); ctx.lineTo(w - 3, 3); ctx.lineTo(w - 3, 8)
                        ctx.moveTo(3, h - 8); ctx.lineTo(3, h - 3); ctx.lineTo(8, h - 3)
                        ctx.moveTo(w - 8, h - 3); ctx.lineTo(w - 3, h - 3); ctx.lineTo(w - 3, h - 8)
                        ctx.stroke()
                        ctx.beginPath(); ctx.arc(w / 2, h / 2, 3.3, 0, Math.PI * 2); ctx.fill()
                    } else if (k === "overview") {
                        ctx.beginPath(); ctx.rect(3, 4, 7, 6); ctx.rect(14, 4, 7, 6)
                        ctx.rect(3, 14, 7, 6); ctx.rect(14, 14, 7, 6); ctx.stroke()
                    } else if (k === "tasks") {
                        ctx.beginPath(); ctx.moveTo(6, 2.5); ctx.lineTo(16, 2.5); ctx.lineTo(20, 6.5)
                        ctx.lineTo(20, 21.5); ctx.lineTo(6, 21.5); ctx.closePath(); ctx.stroke()
                        ctx.beginPath(); ctx.moveTo(16, 2.5); ctx.lineTo(16, 6.5); ctx.lineTo(20, 6.5)
                        ctx.moveTo(9, 11); ctx.lineTo(17, 11); ctx.moveTo(9, 15); ctx.lineTo(17, 15)
                        ctx.moveTo(9, 19); ctx.lineTo(14, 19); ctx.stroke()
                    } else if (k === "accounts") {
                        ctx.beginPath(); ctx.arc(9, 7, 3.2, 0, Math.PI * 2); ctx.arc(17, 8, 2.7, 0, Math.PI * 2); ctx.stroke()
                        ctx.beginPath(); ctx.moveTo(3.5, 20); ctx.quadraticCurveTo(4, 13.5, 9, 13.5); ctx.quadraticCurveTo(14, 13.5, 14.5, 20)
                        ctx.moveTo(13, 14.5); ctx.quadraticCurveTo(17, 12.5, 20.5, 17.5); ctx.stroke()
                    } else if (k === "leads") {
                        ctx.beginPath(); ctx.arc(12, 7, 3.6, 0, Math.PI * 2); ctx.stroke()
                        ctx.beginPath(); ctx.moveTo(4, 20.5); ctx.quadraticCurveTo(5, 13.5, 12, 13.5); ctx.quadraticCurveTo(19, 13.5, 20, 20.5); ctx.stroke()
                        ctx.beginPath(); ctx.moveTo(5, 10); ctx.lineTo(2.5, 13); ctx.lineTo(5, 16); ctx.stroke()
                    } else if (k === "interaction") {
                        ctx.beginPath(); ctx.moveTo(4, 5); ctx.quadraticCurveTo(4, 3, 7, 3); ctx.lineTo(19, 3)
                        ctx.quadraticCurveTo(21, 3, 21, 5); ctx.lineTo(21, 14); ctx.quadraticCurveTo(21, 16, 18, 16)
                        ctx.lineTo(10, 16); ctx.lineTo(5, 20); ctx.lineTo(6, 16); ctx.quadraticCurveTo(4, 16, 4, 14); ctx.closePath(); ctx.stroke()
                        ctx.beginPath(); ctx.arc(9, 9.5, 0.8, 0, Math.PI * 2); ctx.arc(12, 9.5, 0.8, 0, Math.PI * 2); ctx.arc(15, 9.5, 0.8, 0, Math.PI * 2); ctx.fill()
                    } else if (k === "publish") {
                        ctx.beginPath(); ctx.moveTo(4, 5); ctx.lineTo(20, 5); ctx.lineTo(20, 19); ctx.lineTo(4, 19); ctx.closePath(); ctx.stroke()
                        ctx.beginPath(); ctx.moveTo(8, 12); ctx.lineTo(16, 12); ctx.moveTo(12, 8); ctx.lineTo(16, 12); ctx.lineTo(12, 16); ctx.stroke()
                    } else if (k === "spark") {
                        ctx.beginPath(); ctx.moveTo(12, 2); ctx.lineTo(13.5, 9); ctx.lineTo(21, 12); ctx.lineTo(13.5, 13.5); ctx.lineTo(12, 21); ctx.lineTo(10.5, 13.5); ctx.lineTo(3, 12); ctx.lineTo(10.5, 9); ctx.closePath(); ctx.stroke()
                    } else if (k === "analytics") {
                        ctx.beginPath(); ctx.moveTo(4, 20); ctx.lineTo(4, 4); ctx.moveTo(4, 20); ctx.lineTo(21, 20); ctx.stroke()
                        ctx.fillRect(8, 14, 2.5, 4); ctx.fillRect(12.5, 10, 2.5, 8); ctx.fillRect(17, 6, 2.5, 12)
                        ctx.beginPath(); ctx.moveTo(7, 11); ctx.lineTo(11, 8); ctx.lineTo(14, 10); ctx.lineTo(20, 4); ctx.stroke()
                    } else if (k === "settings") {
                        ctx.beginPath(); ctx.arc(12, 12, 4, 0, Math.PI * 2); ctx.stroke()
                        for (var i = 0; i < 8; i++) {
                            var a = i * Math.PI / 4
                            var x1 = 12 + Math.cos(a) * 7
                            var y1 = 12 + Math.sin(a) * 7
                            var x2 = 12 + Math.cos(a) * 10
                            var y2 = 12 + Math.sin(a) * 10
                            ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke()
                        }
                        ctx.beginPath(); ctx.arc(12, 12, 10, 0, Math.PI * 2); ctx.stroke()
                    } else {
                        ctx.beginPath(); ctx.arc(12, 8, 3, 0, Math.PI * 2); ctx.arc(12, 17, 5, 0, Math.PI, true); ctx.stroke()
                    }
                }
            }
            onKindChanged: lineCanvas.requestPaint()
            onTintChanged: lineCanvas.requestPaint()
        }
    }

    // 各平台的矢量兜底标识。正式显示优先使用用户提供的高清素材，
    // 这样左侧列表与各页面的来源图标会和发布包内的实际图标保持一致。
    Component {
        id: platformIconComponent
        Item {
            property string platform: ""
            implicitWidth: 24
            implicitHeight: 24
            Canvas {
                id: platformCanvas
                anchors.fill: parent
                antialiasing: true
                onPaint: {
                    var ctx = getContext("2d")
                    ctx.clearRect(0, 0, width, height)
                    var p = parent.platform
                    var bg = p === "douyin" ? "#171426" : (p === "xhs" ? "#e83e72" : (p === "bilibili" ? "#f47fa5" : (p === "weibo" ? "#f7c64d" : (p === "kuaishou" ? "#ff5a62" : "#52627a"))))
                    ctx.fillStyle = bg
                    ctx.beginPath(); ctx.arc(width / 2, height / 2, 11, 0, Math.PI * 2); ctx.fill()
                    ctx.fillStyle = "#ffffff"
                    ctx.strokeStyle = "#ffffff"
                    ctx.lineWidth = 1.7
                    ctx.lineCap = "round"
                    if (p === "douyin") {
                        ctx.beginPath(); ctx.moveTo(13, 5); ctx.lineTo(13, 15); ctx.quadraticCurveTo(13, 19, 9, 19); ctx.quadraticCurveTo(6, 19, 6, 16); ctx.quadraticCurveTo(6, 13, 10, 13); ctx.stroke()
                        ctx.beginPath(); ctx.moveTo(13, 5); ctx.quadraticCurveTo(15, 8, 18, 8); ctx.stroke()
                    } else if (p === "xhs") {
                        ctx.font = "bold 12px " + (Qt.platform.os === "osx" ? "PingFang SC" : "Microsoft YaHei UI")
                        ctx.fillText("书", 7.3, 16.5)
                    } else if (p === "bilibili") {
                        ctx.font = "bold 13px Arial"
                        ctx.fillText("B", 7.2, 16.5)
                    } else if (p === "weibo") {
                        ctx.beginPath(); ctx.ellipse(5, 8, 14, 8); ctx.stroke()
                        ctx.beginPath(); ctx.arc(12, 8, 3, 0, Math.PI * 2); ctx.fill()
                        ctx.beginPath(); ctx.arc(18, 5, 2, 0, Math.PI * 2); ctx.fill()
                    } else if (p === "kuaishou") {
                        ctx.font = "bold 11px " + (Qt.platform.os === "osx" ? "PingFang SC" : "Microsoft YaHei UI")
                        ctx.fillText("快", 6.4, 16.2)
                    } else if (p === "tieba") {
                        ctx.font = "bold 11px " + (Qt.platform.os === "osx" ? "PingFang SC" : "Microsoft YaHei UI")
                        ctx.fillText("吧", 6.4, 16.2)
                    } else {
                        ctx.font = "bold 12px Arial"
                        ctx.fillText("?", 8.2, 16.5)
                    }
                }
            }
            onPlatformChanged: platformCanvas.requestPaint()
        }
    }

    // 用户提供的高清平台图标。图片文件随项目发布，不能直接引用用户电脑的
    // Pictures 路径，否则换机器或打包后会丢失图标。
    Component {
        id: userPlatformIconComponent
        Item {
            id: userPlatformIcon
            property string platform: ""
            implicitWidth: 24
            implicitHeight: 24
            Rectangle {
                anchors.fill: parent
                radius: width * 0.22
                color: "transparent"
                clip: true
                Rectangle {
                    anchors.fill: parent
                    visible: userPlatformIcon.platform === "kuaishou"
                    radius: width * 0.22
                    color: "#ff5a62"
                    Text {
                        anchors.centerIn: parent
                        text: "快"
                        color: "white"
                        font.pixelSize: Math.max(10, parent.width * 0.48)
                        font.bold: true
                    }
                }
                Rectangle {
                    anchors.fill: parent
                    visible: userPlatformIcon.platform === "tieba"
                    radius: width * 0.22
                    color: "#4a78b8"
                    Text {
                        anchors.centerIn: parent
                        text: "吧"
                        color: "white"
                        font.pixelSize: Math.max(10, parent.width * 0.48)
                        font.bold: true
                    }
                }
                Image {
                    id: platformAssetImage
                    anchors.fill: parent
                    source: {
                        if (userPlatformIcon.platform === "douyin") return "../../../assets/user_platform_douyin.png"
                        if (userPlatformIcon.platform === "xhs") return "../../../assets/user_platform_xhs_transparent.png"
                        if (userPlatformIcon.platform === "bilibili") return "../../../assets/user_platform_bilibili.png"
                        if (userPlatformIcon.platform === "weibo") return "../../../assets/user_platform_weibo_transparent.png"
                        return ""
                    }
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    mipmap: true
                    asynchronous: true
                }
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        Rectangle {
            Layout.preferredWidth: 216
            Layout.fillHeight: true
            color: "#0b1220"
            border.color: window.line
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 14
                spacing: 6
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 72
                    color: "transparent"
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 8
                        spacing: 10
                        Rectangle {
                            Layout.preferredWidth: 34
                            Layout.preferredHeight: 34
                            radius: 10
                            color: "transparent"
                            clip: true
                            Image {
                                anchors.fill: parent
                                source: "../../../assets/user_app_icon_transparent.png"
                                fillMode: Image.PreserveAspectFit
                                smooth: true
                                mipmap: true
                                asynchronous: true
                            }
                        }
                        Column {
                            Layout.fillWidth: true
                            spacing: 2
                            Text { text: "采集与发布"; color: window.ink; font.pixelSize: 16; font.weight: Font.DemiBold }
                            Text { text: window.appVersion; color: window.blue; font.pixelSize: 10 }
                        }
                    }
                }
                // 左上角工作区切换：始终只保留一个明确的入口，避免把发布
                // 页面混在采集页签中造成误操作。
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 38
                    radius: 9
                    color: "#101b2d"
                    border.color: window.line
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 3
                        spacing: 3
                        AppButton {
                            text: "采集"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            onClicked: window.switchWorkspace("collect")
                            contentItem: Text { text: parent.text; color: window.workspaceMode === "collect" ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11; font.weight: window.workspaceMode === "collect" ? Font.DemiBold : Font.Normal }
                            background: Rectangle { radius: 7; color: window.workspaceMode === "collect" ? "#274b83" : (parent.hovered ? "#182b49" : "transparent"); border.color: window.workspaceMode === "collect" ? "#4776aa" : "transparent" }
                        }
                        AppButton {
                            text: "发布"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            onClicked: window.switchWorkspace("publish")
                            contentItem: Text { text: parent.text; color: window.workspaceMode === "publish" ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11; font.weight: window.workspaceMode === "publish" ? Font.DemiBold : Font.Normal }
                            background: Rectangle { radius: 7; color: window.workspaceMode === "publish" ? "#274b83" : (parent.hovered ? "#182b49" : "transparent"); border.color: window.workspaceMode === "publish" ? "#4776aa" : "transparent" }
                        }
                    }
                }
                Text { visible: window.workspaceMode === "collect"; text: "工作区"; color: "#60748f"; font.pixelSize: 11; Layout.leftMargin: 10; Layout.topMargin: 3; Layout.bottomMargin: 3 }
                Repeater {
                    visible: window.workspaceMode === "collect"
                    model: [
                        {key: "overview", label: "采集总览", badge: ""},
                        {key: "tasks", label: "任务中心", badge: ""},
                        {key: "accounts", label: "账号管理", badge: ""},
                        {key: "leads", label: "线索中心", badge: ""},
                        {key: "interaction", label: "互动中心", badge: ""}
                    ]
                    delegate: AppButton {
                        // Repeater 的代理是独立项目，不能只隐藏 Repeater；
                        // 代理本身也要收起，保证进入发布工作区后下方采集 TAB 不残留。
                        visible: window.workspaceMode === "collect"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 44
                        onClicked: window.showPage(modelData.key)
                        contentItem: RowLayout {
                            spacing: 12
                            Loader { Layout.preferredWidth: 24; Layout.preferredHeight: 24; sourceComponent: lineIconComponent; onLoaded: { item.kind = modelData.key; item.tint = window.blue } }
                            Text { text: modelData.label; color: backend.currentPage === modelData.key ? window.ink : window.muted; Layout.fillWidth: true; verticalAlignment: Text.AlignVCenter; font.pixelSize: 13 }
                            Text {
                                text: modelData.key === "tasks" ? window.overviewValue("tasks") : (modelData.key === "leads" ? window.overviewValue("leads") : (modelData.key === "interaction" ? window.overviewValue("interactions") : ""))
                                visible: text !== "" && text !== "0"
                                color: window.ink
                                font.pixelSize: 10
                            }
                        }
                        background: Rectangle { radius: 9; color: backend.currentPage === modelData.key ? "#182748" : "transparent"; border.color: backend.currentPage === modelData.key ? "#314a83" : "transparent" }
                    }
                }
                Text { visible: window.workspaceMode === "publish"; text: "发布工作区"; color: "#60748f"; font.pixelSize: 11; Layout.leftMargin: 10; Layout.topMargin: 10; Layout.bottomMargin: 3 }
                Repeater {
                    visible: window.workspaceMode === "publish"
                    model: [
                        {key: "account", label: "账号信息", icon: "accounts"},
                        {key: "generation", label: "内容生成", icon: "spark"},
                        {key: "publish", label: "发布", icon: "publish"},
                        {key: "messages", label: "消息中心", icon: "interaction"},
                        {key: "settings", label: "设置", icon: "settings"}
                    ]
                    delegate: AppButton {
                        visible: window.workspaceMode === "publish"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 44
                        onClicked: publishPage.selectTab(modelData.key)
                        contentItem: RowLayout {
                            spacing: 12
                            Loader { Layout.preferredWidth: 24; Layout.preferredHeight: 24; sourceComponent: lineIconComponent; onLoaded: { item.kind = modelData.icon; item.tint = window.blue } }
                            Text { text: modelData.label; color: publishPage.publishTab === modelData.key ? window.ink : window.muted; Layout.fillWidth: true; verticalAlignment: Text.AlignVCenter; font.pixelSize: 13 }
                            Text { visible: modelData.key === "messages" && backend.publishMessageUnread > 0; text: backend.publishMessageUnread; color: window.ink; font.pixelSize: 10 }
                        }
                        background: Rectangle { radius: 9; color: publishPage.publishTab === modelData.key ? "#182748" : "transparent"; border.color: publishPage.publishTab === modelData.key ? "#314a83" : "transparent" }
                    }
                }
                Text { visible: window.workspaceMode === "collect"; text: "管理"; color: "#60748f"; font.pixelSize: 11; Layout.leftMargin: 10; Layout.topMargin: 18; Layout.bottomMargin: 3 }
                AppButton {
                    // 数据分析已从工作台导航移除；保留页面实现仅用于兼容旧状态数据。
                    visible: false
                    Layout.fillWidth: true
                    Layout.preferredHeight: 44
                    onClicked: window.showPage("analytics")
                    contentItem: RowLayout {
                        spacing: 12
                        Loader { Layout.preferredWidth: 24; Layout.preferredHeight: 24; sourceComponent: lineIconComponent; onLoaded: { item.kind = "analytics"; item.tint = window.blue } }
                        Text { text: "数据分析"; color: backend.currentPage === "analytics" ? window.ink : window.muted; Layout.fillWidth: true; font.pixelSize: 13 }
                    }
                    background: Rectangle { radius: 9; color: backend.currentPage === "analytics" ? "#182748" : "transparent"; border.color: backend.currentPage === "analytics" ? "#314a83" : "transparent" }
                }
                AppButton {
                    visible: window.workspaceMode === "collect"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 44
                    onClicked: window.showPage("diagnostics")
                    contentItem: RowLayout {
                        spacing: 12
                        Loader { Layout.preferredWidth: 24; Layout.preferredHeight: 24; sourceComponent: lineIconComponent; onLoaded: { item.kind = "settings"; item.tint = window.blue } }
                        Text { text: "设置与诊断"; color: backend.currentPage === "diagnostics" ? window.ink : window.muted; Layout.fillWidth: true; font.pixelSize: 13 }
                    }
                    background: Rectangle { radius: 9; color: backend.currentPage === "diagnostics" ? "#182748" : "transparent"; border.color: backend.currentPage === "diagnostics" ? "#314a83" : "transparent" }
                }
                AppButton {
                    // 管理员入口统一收纳到个人中心，避免侧栏重复占位。
                    visible: false
                    Layout.fillWidth: true
                    Layout.preferredHeight: 40
                    onClicked: { backend.refreshAuthUsers(); adminUsersDialog.open() }
                    contentItem: RowLayout {
                        spacing: 12
                        Loader { Layout.preferredWidth: 24; Layout.preferredHeight: 24; sourceComponent: lineIconComponent; onLoaded: { item.kind = "accounts"; item.tint = window.blue } }
                        Text { text: "用户审批"; color: window.muted; Layout.fillWidth: true; verticalAlignment: Text.AlignVCenter; font.pixelSize: 13 }
                    }
                    background: Rectangle { radius: 9; color: parent.hovered ? "#182748" : "transparent"; border.color: parent.hovered ? "#314a83" : "transparent" }
                }
                AppButton {
                    // 管理员入口统一收纳到个人中心，避免侧栏重复占位。
                    visible: false
                    Layout.fillWidth: true
                    Layout.preferredHeight: 40
                    onClicked: {
                        backend.refreshAdminDashboard()
                        adminCenterDialog.open()
                    }
                    contentItem: RowLayout {
                        spacing: 12
                        Loader { Layout.preferredWidth: 24; Layout.preferredHeight: 24; sourceComponent: lineIconComponent; onLoaded: { item.kind = "analytics"; item.tint = window.blue } }
                        Text { text: "管理员中心"; color: window.muted; Layout.fillWidth: true; verticalAlignment: Text.AlignVCenter; font.pixelSize: 13 }
                    }
                    background: Rectangle { radius: 9; color: parent.hovered ? "#182748" : "transparent"; border.color: parent.hovered ? "#314a83" : "transparent" }
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 148
                    Layout.topMargin: 12
                    radius: 10
                    color: "#101b2d"
                    border.color: window.line
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 5
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "运行日志"; color: window.ink; font.pixelSize: 11; font.weight: Font.Medium; Layout.fillWidth: true }
                            Text { text: "实时"; color: window.green; font.pixelSize: 10 }
                        }
                        ListView {
                            id: sidebarLogList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 1
                            model: backend.diagnosticLogs
                            onCountChanged: if (count > 0) positionViewAtEnd()
                            delegate: Text {
                                width: sidebarLogList.width
                                height: 22
                                text: "[" + String(modelData.timestamp || "").slice(-8) + "] " + String(modelData.message || "")
                                color: modelData.level === "error" ? "#ff9b9b" : (String(modelData.message || "").indexOf("人工") >= 0 ? window.amber : window.muted)
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 9
                            }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 84
                    radius: 10
                    color: "#111d30"
                    border.color: window.line
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 6
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 7
                            Rectangle { width: 7; height: 7; radius: 4; color: backend.connectionText === "后台服务已连接" ? window.green : window.amber }
                            Text { text: backend.connectionText; color: window.ink; font.pixelSize: 11; Layout.fillWidth: true }
                        }
                        Text { text: "六个平台适配器"; color: window.muted; font.pixelSize: 10 }
                        Text { text: "后台服务实时维护"; color: window.muted; font.pixelSize: 10 }
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0
            Rectangle {
                Layout.fillWidth: true
                // 发布中心采用自己的全屏工作台布局；隐藏全局顶部栏后，
                // 发布中心才能与视觉稿中的左侧导航 + 主工作区完全对齐。
                Layout.preferredHeight: backend.currentPage === "publish" ? 0 : 68
                Layout.minimumHeight: 0
                Layout.maximumHeight: backend.currentPage === "publish" ? 0 : 68
                visible: backend.currentPage !== "publish"
                color: "#0d1727"
                border.color: window.line
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 26
                    anchors.rightMargin: 26
                    spacing: 16
                    Text { text: backend.pageLabel; color: window.ink; font.pixelSize: 15; font.weight: Font.DemiBold }
                    Text { text: "/  全部结果"; color: window.muted; font.pixelSize: 12 }
                    Text { visible: backend.lastError !== ""; text: backend.lastError; color: "#ff9b9b"; Layout.preferredWidth: 250; elide: Text.ElideRight; font.pixelSize: 10 }
                    Item { Layout.fillWidth: true }
                    AppButton {
                        id: personalCenterButton
                        Layout.preferredWidth: 132
                        Layout.preferredHeight: 38
                        text: backend.authEmployeeName || backend.authUsername || "个人中心"
                        onClicked: personalCenterDialog.open()
                        contentItem: RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 9
                            anchors.rightMargin: 9
                            spacing: 8
                            Rectangle {
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                                radius: 12
                                color: "#5f7cff"
                                Text {
                                    anchors.centerIn: parent
                                    text: String(personalCenterButton.text || "人").slice(0, 1)
                                    color: "#ffffff"
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                            }
                            Text {
                                text: personalCenterButton.text
                                color: window.ink
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 11
                            }
                        }
                        background: Rectangle {
                            radius: 9
                            color: personalCenterButton.hovered ? "#182748" : "#111d30"
                            border.color: personalCenterButton.hovered ? window.blue : window.line
                        }
                    }
                }
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: backend.currentPage !== "publish" && window.humanWaitingText() !== "" ? 44 : 0
                Layout.minimumHeight: 0
                Layout.maximumHeight: backend.currentPage !== "publish" && window.humanWaitingText() !== "" ? 44 : 0
                visible: backend.currentPage !== "publish" && window.humanWaitingText() !== ""
                color: "#3b2a22"
                border.color: "#8e633d"
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 18
                    anchors.rightMargin: 18
                    spacing: 10
                    Text { text: "⚠"; color: window.amber; font.pixelSize: 18 }
                    Text {
                        text: "检测到人工验证：" + window.humanWaitingText() + "。请处理对应浏览器，完成后点击任务“继续”。"
                        color: "#ffe0b2"
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        font.pixelSize: 11
                    }
                    AppButton {
                        text: "查看账号"
                        onClicked: window.showPage("accounts")
                        contentItem: Text { text: parent.text; color: window.amber; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                        background: Rectangle { radius: 6; color: "#4a3324"; border.color: parent.hovered ? window.amber : "#8e633d" }
                    }
                }
            }
            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: backend.pageIndex

            Page {
                id: overviewPage
                background: Rectangle { color: window.color }
                property var platformSlots: [
                    {key: "douyin", label: "抖音", enabled: true, color: "#2f8cff"},
                    {key: "xhs", label: "小红书", enabled: true, color: "#ff496d"},
                    {key: "bilibili", label: "B站", enabled: true, color: "#35b8ef"},
                    {key: "weibo", label: "微博", enabled: true, color: "#f0b33f"},
                    {key: "kuaishou", label: "快手", enabled: true, color: "#ff765d"},
                    {key: "tieba", label: "百度贴吧", enabled: true, color: "#4a78b8"},
                ]
                // 单次状态推送只解析一次总览快照，避免每个 KPI/图表重复解析。
                property var summary: JSON.parse(backend.snapshotJson || "{}")
                property bool showAlerts: false
                // 任务队列的表头和数据行共用同一组列宽，避免 RowLayout
                // 根据文字长度重新分配空间导致上下错列。
                property int queueKeywordWidth: 185
                property int queuePlatformWidth: 120
                // 账号列预留更宽的固定空间，保证进度、评论数、状态
                // 与前面的账号信息有清晰间隔，并且上下行坐标一致。
                property int queueAccountWidth: 250
                property int queueProgressWidth: 160
                property int queueCommentsWidth: 88
                property int queueLeftInset: 7
                property int queueRightInset: 5
                function platformMatches(row, key) {
                    var value = String(row.platform || "").toLowerCase()
                    if (key === "douyin") return ["douyin", "dy", "抖音"].indexOf(value) >= 0
                    if (key === "xhs") return ["xhs", "xiaohongshu", "小红书"].indexOf(value) >= 0
                    if (key === "bilibili") return ["bilibili", "b站"].indexOf(value) >= 0
                    if (key === "kuaishou") return ["kuaishou", "ks", "快手"].indexOf(value) >= 0
                    return value === key
                }
                function platformStats(slot) {
                    var rows = backend.taskRows || []
                    var tasks = 0; var comments = 0
                    for (var i = 0; i < rows.length; i++) {
                        if (!platformMatches(rows[i], slot.key)) continue
                        tasks += 1
                        comments += Number(rows[i].comments || 0)
                    }
                    return {tasks: tasks, comments: comments}
                }
                function pendingTasks() {
                    var total = Number(summary.tasks || 0) - Number(summary.running_tasks || 0) - Number(summary.tasks_done || 0)
                    return Math.max(0, total)
                }
                function trendValues() {
                    var rows = backend.taskRows || []
                    var values = []
                    for (var i = 0; i < 12; i++) {
                        var total = 0
                        for (var j = i; j < rows.length; j += 12) total += Number(rows[j].comments || 0)
                        values.push(total)
                    }
                    return values
                }
                function recentLogs() {
                    var source = backend.diagnosticLogs || []
                    var rows = []
                    for (var i = Math.max(0, source.length - 6); i < source.length; i++) rows.push(source[i])
                    rows.reverse()
                    return rows
                }
                function alertLogs() {
                    var source = backend.diagnosticLogs || []
                    var rows = []
                    for (var i = source.length - 1; i >= 0 && rows.length < 6; i--) {
                        var level = String(source[i].level || "").toLowerCase()
                        if (level === "warning" || level === "error") rows.push(source[i])
                    }
                    return rows
                }
                Flickable {
                    id: overviewScroll
                    anchors.fill: parent
                    contentWidth: width
                    contentHeight: overviewColumn.implicitHeight + 52
                    clip: true
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    ColumnLayout {
                        id: overviewColumn
                        x: 30; y: 24
                        width: overviewScroll.width - 60
                        spacing: 14
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "采集总览"; color: window.ink; font.pixelSize: 26; font.weight: Font.DemiBold }
                            Text { text: "实时运营驾驶舱"; color: window.muted; font.pixelSize: 12; Layout.leftMargin: 10 }
                            Item { Layout.fillWidth: true }
                            Rectangle {
                                Layout.preferredWidth: 92; Layout.preferredHeight: 30; radius: 15
                                color: backend.connectionText === "后台服务已连接" ? "#123b35" : "#3c3020"
                                border.color: backend.connectionText === "后台服务已连接" ? "#2b8e76" : window.amber
                                RowLayout { anchors.fill: parent; anchors.leftMargin: 10; anchors.rightMargin: 10; spacing: 6
                                    Rectangle { width: 7; height: 7; radius: 4; color: backend.connectionText === "后台服务已连接" ? window.green : window.amber }
                                    Text { text: backend.connectionText === "后台服务已连接" ? "已连接" : "需检查"; color: window.ink; font.pixelSize: 10 }
                                }
                            }
                            Text { text: "最后更新 " + String(backend.beijingNowText || "").slice(0, 16); color: window.muted; font.pixelSize: 10 }
                            AppButton {
                                text: "局部刷新"; Layout.preferredWidth: 82; Layout.preferredHeight: 30
                                onClicked: backend.refresh()
                                contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                background: Rectangle { radius: 7; color: parent.hovered ? "#203857" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                            }
                            AppButton {
                                text: "＋ 新建任务"; Layout.preferredWidth: 104; Layout.preferredHeight: 30
                                onClicked: taskDialog.open()
                                contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10; font.weight: Font.DemiBold }
                                background: Rectangle { radius: 7; color: parent.hovered ? "#8bbaff" : window.blue }
                            }
                        }
                        Text { text: "采集、评论、线索与互动状态在同一屏观察；后台推送只更新变化的卡片和行。"; color: window.muted; font.pixelSize: 11 }
                        GridLayout {
                            Layout.fillWidth: true; columns: 4; columnSpacing: 10; rowSpacing: 10
                            Repeater {
                                model: [
                                    {label: "进行中", key: "running_tasks", color: "#2f8cff", hint: "当前运行任务"},
                                    {label: "已完成", key: "completed_videos", color: "#35d69a", hint: "有效作品"},
                                    {label: "评论入库", key: "comments", color: "#45d5a1", hint: "已采集评论"},
                                    {label: "待处理", key: "pending_tasks", color: "#f3a62f", hint: "等待开始 / 人工"}
                                ]
                                delegate: Rectangle {
                                    Layout.fillWidth: true; Layout.preferredHeight: 132; radius: 10
                                    color: "#0e1b2d"; border.color: "#203957"
                                    property color accent: modelData.color
                                    MouseArea { anchors.fill: parent; hoverEnabled: true; onClicked: window.showPage("tasks") }
                                    Rectangle { x: 14; y: 14; width: 42; height: 42; radius: 21; color: accent; opacity: 0.16 }
                                    Text { x: 14; y: 14; width: 42; height: 42; text: modelData.key === "running_tasks" ? "▶" : (modelData.key === "completed_videos" ? "✓" : "⌛"); color: accent; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 19; font.weight: Font.DemiBold }
                                    Column { x: 70; y: 17; spacing: 2
                                        Text { text: modelData.label; color: window.muted; font.pixelSize: 11 }
                                        Text { text: modelData.key === "pending_tasks" ? overviewPage.pendingTasks() : (overviewPage.summary[modelData.key] || 0); color: window.ink; font.pixelSize: 27; font.weight: Font.DemiBold }
                                        Text { text: modelData.hint; color: accent; font.pixelSize: 9 }
                                    }
                                    Row { x: 16; y: 105; spacing: 3
                                        Repeater { model: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
                                            delegate: Rectangle { width: 4; height: 8 + ((index + Number(overviewPage.summary.comments || 0)) % 6) * 3; radius: 2; color: accent; opacity: 0.45 }
                                        }
                                    }
                                }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true; spacing: 12
                            Rectangle {
                                Layout.fillWidth: true; Layout.preferredHeight: 386; radius: 10; color: "#0e1b2d"; border.color: "#203957"
                                ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 9
                                    RowLayout { Layout.fillWidth: true
                                        Text { text: "任务队列"; color: window.ink; font.pixelSize: 17; font.weight: Font.Medium }
                                        Text { text: "实时状态"; color: window.muted; font.pixelSize: 10; Layout.leftMargin: 7 }
                                        Item { Layout.fillWidth: true }
                                        Text { text: (overviewPage.summary.tasks || 0) + " 个任务"; color: window.muted; font.pixelSize: 10 }
                                    }
                                    Item { Layout.fillWidth: true; Layout.preferredHeight: 20
                                        Text { x: overviewPage.queueLeftInset; width: overviewPage.queueKeywordWidth; text: "关键词"; color: window.muted; horizontalAlignment: Text.AlignLeft; font.pixelSize: 10 }
                                        Text { x: overviewPage.queueLeftInset + overviewPage.queueKeywordWidth + (overviewPage.queuePlatformWidth - implicitWidth) / 2; width: implicitWidth; text: "平台"; color: window.muted; font.pixelSize: 10 }
                                        Text { x: overviewPage.queueLeftInset + overviewPage.queueKeywordWidth + overviewPage.queuePlatformWidth + (overviewPage.queueAccountWidth - implicitWidth) / 2; width: implicitWidth; text: "账号"; color: window.muted; font.pixelSize: 10 }
                                        Text { x: overviewPage.queueLeftInset + overviewPage.queueKeywordWidth + overviewPage.queuePlatformWidth + overviewPage.queueAccountWidth + (overviewPage.queueProgressWidth - implicitWidth) / 2; width: implicitWidth; text: "进度"; color: window.muted; font.pixelSize: 10 }
                                        Text { x: overviewPage.queueLeftInset + overviewPage.queueKeywordWidth + overviewPage.queuePlatformWidth + overviewPage.queueAccountWidth + overviewPage.queueProgressWidth + (overviewPage.queueCommentsWidth - implicitWidth) / 2; width: implicitWidth; text: "评论数"; color: window.muted; font.pixelSize: 10 }
                                        Text { x: overviewPage.queueLeftInset + overviewPage.queueKeywordWidth + overviewPage.queuePlatformWidth + overviewPage.queueAccountWidth + overviewPage.queueProgressWidth + overviewPage.queueCommentsWidth + (Math.max(0, parent.width - overviewPage.queueLeftInset - overviewPage.queueKeywordWidth - overviewPage.queuePlatformWidth - overviewPage.queueAccountWidth - overviewPage.queueProgressWidth - overviewPage.queueCommentsWidth - overviewPage.queueRightInset) - implicitWidth) / 2; width: implicitWidth; text: "状态"; color: window.muted; font.pixelSize: 10 }
                                    }
                                    Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
                                    ListView {
                                        id: overviewTaskList; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 1
                                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                                        model: backend.taskRows
                                        delegate: Rectangle {
                                            property var taskRow: modelData
                                            property var taskAccounts: (taskRow.account_names && taskRow.account_names.length > 0) ? taskRow.account_names : ["自动分配"]
                                            property int accountLineCount: Math.max(1, taskAccounts.length)
                                            width: overviewTaskList.width
                                            height: Math.max(44, 14 + accountLineCount * 18)
                                            radius: 3
                                            color: index % 2 === 0 ? "#0e1b2d" : "#12253b"
                                            property int tablePlatformX: overviewPage.queueLeftInset + overviewPage.queueKeywordWidth
                                            property int tableAccountX: tablePlatformX + overviewPage.queuePlatformWidth
                                            property int tableProgressX: tableAccountX + overviewPage.queueAccountWidth
                                            property int tableCommentsX: tableProgressX + overviewPage.queueProgressWidth
                                            property int tableStatusX: tableCommentsX + overviewPage.queueCommentsWidth
                                            Item { anchors.fill: parent
                                                Text {
                                                    x: overviewPage.queueLeftInset; width: overviewPage.queueKeywordWidth; height: 20
                                                    anchors.verticalCenter: parent.verticalCenter
                                                    text: taskRow.keyword || "未命名任务"; color: window.ink; elide: Text.ElideRight
                                                    verticalAlignment: Text.AlignVCenter; font.pixelSize: 10
                                                }
                                                Loader {
                                                    x: tablePlatformX; y: (parent.height - 19) / 2; width: 19; height: 19
                                                    sourceComponent: userPlatformIconComponent
                                                    onLoaded: item.platform = taskRow.platform
                                                }
                                                Text {
                                                    x: tablePlatformX + 27; y: (parent.height - 20) / 2; width: overviewPage.queuePlatformWidth - 27; height: 20
                                                    text: taskRow.platform_label || "未知"; color: window.blue; elide: Text.ElideRight
                                                    verticalAlignment: Text.AlignVCenter; font.pixelSize: 9
                                                }
                                                Column {
                                                    x: tableAccountX; y: Math.max(0, (parent.height - accountLineCount * 18) / 2)
                                                    width: overviewPage.queueAccountWidth; height: accountLineCount * 18; spacing: 0
                                                    Repeater {
                                                        model: taskAccounts
                                                        delegate: Text {
                                                            width: overviewPage.queueAccountWidth; height: 18; text: modelData
                                                            color: window.muted; elide: Text.ElideRight
                                                            verticalAlignment: Text.AlignVCenter; font.pixelSize: 9
                                                        }
                                                    }
                                                }
                                                Rectangle {
                                                    x: tableProgressX; y: (parent.height - 18) / 2
                                                    width: overviewPage.queueProgressWidth; height: 18
                                                    radius: 9; color: "#0c1728"; border.color: "#294566"; clip: true
                                                    Rectangle {
                                                        width: parent.width * Math.max(0, Math.min(1, Number(taskRow.progress || 0) / 100))
                                                        height: parent.height; radius: 9
                                                        gradient: Gradient {
                                                            GradientStop { position: 0.0; color: "#327eff" }
                                                            GradientStop { position: 0.55; color: "#39a9ff" }
                                                            GradientStop { position: 1.0; color: "#45d5a1" }
                                                        }
                                                    }
                                                    Text {
                                                        anchors.fill: parent; text: Number(taskRow.progress || 0) + "%"
                                                        color: window.ink; horizontalAlignment: Text.AlignHCenter
                                                        verticalAlignment: Text.AlignVCenter; font.pixelSize: 9; font.weight: Font.DemiBold
                                                    }
                                                }
                                                Text {
                                                    x: tableCommentsX; y: (parent.height - 20) / 2; width: overviewPage.queueCommentsWidth; height: 20
                                                    text: Number(taskRow.comments || 0); color: window.ink
                                                    horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 9
                                                }
                                                Text {
                                                    x: tableStatusX; y: (parent.height - 20) / 2
                                                    width: Math.max(0, parent.width - tableStatusX - overviewPage.queueRightInset); height: 20
                                                    text: window.taskStatusLabel(taskRow); color: window.muted; elide: Text.ElideRight
                                                    horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 9
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            Rectangle {
                                Layout.preferredWidth: 305; Layout.preferredHeight: 386; radius: 10; color: "#0e1b2d"; border.color: "#203957"
                                ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 10
                                    RowLayout {
                                        Layout.fillWidth: true
                                        AppButton {
                                            text: "实时事件"; Layout.preferredWidth: 82; Layout.preferredHeight: 29
                                            onClicked: overviewPage.showAlerts = false
                                            contentItem: Text { text: parent.text; color: overviewPage.showAlerts ? window.muted : window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12; font.weight: Font.Medium }
                                            background: Rectangle { color: overviewPage.showAlerts ? "transparent" : "#16345b"; border.color: overviewPage.showAlerts ? "transparent" : window.blue; radius: 5 }
                                        }
                                        AppButton {
                                            text: "异常与提醒"; Layout.preferredWidth: 92; Layout.preferredHeight: 29
                                            onClicked: overviewPage.showAlerts = true
                                            contentItem: Text { text: parent.text; color: overviewPage.showAlerts ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12; font.weight: Font.Medium }
                                            background: Rectangle { color: overviewPage.showAlerts ? "#16345b" : "transparent"; border.color: overviewPage.showAlerts ? window.amber : "transparent"; radius: 5 }
                                        }
                                        Item { Layout.fillWidth: true }
                                        Text { text: "局部更新"; color: window.green; font.pixelSize: 9 }
                                    }
                                    ListView { Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 7; model: overviewPage.showAlerts ? overviewPage.alertLogs() : overviewPage.recentLogs()
                                        delegate: Rectangle { width: parent ? parent.width : 0; height: 48; radius: 7; color: modelData.level === "error" ? "#301d2a" : (modelData.level === "warning" ? "#302719" : "#101f33"); border.color: modelData.level === "error" ? "#824359" : (modelData.level === "warning" ? "#806128" : "#24405e")
                                            RowLayout { anchors.fill: parent; anchors.leftMargin: 9; anchors.rightMargin: 7; spacing: 6
                                                Rectangle {
                                                    width: 22; height: 22; radius: 11
                                                    color: modelData.level === "error" ? "#ef5367" : (modelData.level === "warning" ? "#e49a28" : "#36d59b")
                                                    Text { anchors.centerIn: parent; text: modelData.level === "error" ? "×" : (modelData.level === "warning" ? "!" : "✓"); color: "#071224"; font.pixelSize: 13; font.weight: Font.DemiBold }
                                                }
                                                ColumnLayout { Layout.fillWidth: true; spacing: 1
                                                    Text { text: String(modelData.message || ""); color: modelData.level === "error" ? "#ff9aaa" : window.ink; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 10 }
                                                    Text { text: String(modelData.timestamp || "").replace("T", " ").slice(-8); color: window.muted; font.pixelSize: 8 }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true; spacing: 12
                            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 248; radius: 10; color: "#0e1b2d"; border.color: "#203957"
                                ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 8
                                    Text { text: "采集趋势"; color: window.ink; font.pixelSize: 17; font.weight: Font.Medium }
                                    Text { text: "按当前任务的评论采集量汇总"; color: window.muted; font.pixelSize: 10 }
                                    Canvas { id: overviewTrendCanvas; Layout.fillWidth: true; Layout.fillHeight: true; antialiasing: true
                                        onPaint: { var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height); var vals = overviewPage.trendValues(); var max = 1; for (var i = 0; i < vals.length; i++) max = Math.max(max, vals[i]); ctx.strokeStyle = "#1d3550"; ctx.lineWidth = 1; for (var g = 1; g < 4; g++) { var gy = height * g / 4; ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(width, gy); ctx.stroke() }; ctx.beginPath(); for (var p = 0; p < vals.length; p++) { var x = p * width / Math.max(1, vals.length - 1); var y = height - (vals[p] / max) * (height - 18) - 5; if (p === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y) }; ctx.strokeStyle = "#2f9cff"; ctx.lineWidth = 2.2; ctx.stroke(); ctx.lineTo(width, height); ctx.lineTo(0, height); ctx.closePath(); ctx.fillStyle = "rgba(47, 156, 255, 0.13)"; ctx.fill(); ctx.beginPath(); for (var q = 0; q < vals.length; q++) { var qx = q * width / Math.max(1, vals.length - 1); var qy = height - (vals[q] / max) * (height - 18) - 5; ctx.moveTo(qx + 2, qy); ctx.arc(qx, qy, 2.5, 0, Math.PI * 2) }; ctx.fillStyle = "#40c1ff"; ctx.fill() }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Text { text: "00:00"; color: window.muted; font.pixelSize: 8 }
                                        Item { Layout.fillWidth: true }
                                        Text { text: "12:00"; color: window.muted; font.pixelSize: 8 }
                                        Item { Layout.fillWidth: true }
                                        Text { text: "当前"; color: window.muted; font.pixelSize: 8 }
                                    }
                                }
                            }
                            Rectangle { Layout.preferredWidth: 370; Layout.preferredHeight: 248; radius: 10; color: "#0e1b2d"; border.color: "#203957"
                                ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 8
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Text { text: "平台分布"; color: window.ink; font.pixelSize: 17; font.weight: Font.Medium }
                                        Item { Layout.fillWidth: true }
                                        Text { text: "评论数"; color: window.muted; font.pixelSize: 9 }
                                    }
                                    RowLayout { Layout.fillWidth: true; Layout.fillHeight: true; spacing: 13
                                        Canvas { id: overviewDonutCanvas; Layout.preferredWidth: 142; Layout.preferredHeight: 142; antialiasing: true
                                            onPaint: { var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height); var total = 0; var vals = []; for (var i = 0; i < overviewPage.platformSlots.length; i++) { var v = overviewPage.platformStats(overviewPage.platformSlots[i]).comments; vals.push(v); total += v }; var colors = ["#2f8cff", "#ff496d", "#35b8ef", "#f0b33f", "#ff765d", "#7186a3"]; var start = -Math.PI / 2; var cx = width / 2; var cy = height / 2; var r = Math.min(width, height) * 0.42; for (var j = 0; j < vals.length; j++) { var part = total > 0 ? vals[j] / total : 1 / Math.max(1, vals.length); ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, r, start, start + part * Math.PI * 2); ctx.closePath(); ctx.fillStyle = colors[j]; ctx.fill(); start += part * Math.PI * 2 }; ctx.globalCompositeOperation = "destination-out"; ctx.beginPath(); ctx.arc(cx, cy, r * 0.54, 0, Math.PI * 2); ctx.fill(); ctx.globalCompositeOperation = "source-over"; ctx.fillStyle = "#0e1b2d"; ctx.font = "bold 14px sans-serif"; ctx.textAlign = "center"; ctx.fillText(String(overviewPage.summary.comments || 0), cx, cy + 5) }
                                        }
                                        ColumnLayout { Layout.fillWidth: true; spacing: 5
                                            Repeater { model: overviewPage.platformSlots
                                                delegate: RowLayout {
                                                    Layout.fillWidth: true; spacing: 5
                                                    Text { text: modelData.enabled ? "●" : "○"; color: modelData.color; font.pixelSize: 11 }
                                                    Text { text: modelData.label + (modelData.enabled ? "" : "（预留）"); color: modelData.enabled ? window.ink : window.muted; Layout.fillWidth: true; font.pixelSize: 9 }
                                                    Text { text: modelData.enabled ? overviewPage.platformStats(modelData).comments : "—"; color: window.muted; font.pixelSize: 9 }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                Connections {
                    target: backend
                    function onViewChanged() { overviewTrendCanvas.requestPaint(); overviewDonutCanvas.requestPaint() }
                }
            }

            Page {
                id: tasksPage
                background: Rectangle { color: window.color }
                property int taskIdWidth: 62
                property int taskPlatformWidth: 116
                property int taskProgressWidth: 238
                property int taskStatusWidth: 112
                property int taskActionsWidth: 390
                property string taskStatusFilter: "all"
                property int taskAnchorId: 0
                property int taskAnchorIndex: -1
                property real taskScrollY: 0
                property real taskLastScrollY: 0
                property bool taskPositionSaved: false
                property bool taskRestoringPosition: false
                property bool showCompletedTasks: false
                property int deletingTaskId: 0
                property int deleteCandidateId: 0
                property string deleteCandidateKeyword: ""
                function requestDelete(task) {
                    if (deletingTaskId !== 0)
                        return
                    deleteCandidateId = Number(task && task.id || 0)
                    deleteCandidateKeyword = String(task && task.keyword || "")
                    if (deleteCandidateId > 0)
                        deleteConfirmDialog.open()
                }
                function confirmDelete() {
                    if (deleteCandidateId <= 0 || deletingTaskId !== 0)
                        return
                    deletingTaskId = deleteCandidateId
                    rememberTaskListPosition(deleteCandidateId)
                    var id = deleteCandidateId
                    deleteCandidateId = 0
                    deleteCandidateKeyword = ""
                    deleteConfirmDialog.close()
                    backend.deleteTask(id)
                }
                function taskMatchesStatus(row, filter) {
                    if (filter === "all") return true
                    var run = row.latest_run || {}
                    var reason = String(run.stop_reason || "") + String(row.error_message || "")
                    if (filter === "no_more")
                        return window.taskStatusLabel(row) === "暂无更多视频" || row.status === "no_more"
                    if (filter === "waiting_human")
                        return row.status === "waiting_human" || reason.indexOf("人工") >= 0 || reason.indexOf("验证") >= 0
                            || String(row.error_reason || "").indexOf("人工") >= 0
                            || String(row.error_reason || "").indexOf("验证") >= 0
                    return String(row.status || "") === filter
                }
                function rememberTaskListPosition(preferredId) {
                    var rows = taskListView.model || []
                    var anchor = Number(preferredId || 0)
                    if (!anchor) {
                        var visibleIndex = taskListView.indexAt(4, Math.max(4, taskListView.height / 3))
                        if (visibleIndex >= 0 && visibleIndex < rows.length)
                            anchor = Number(rows[visibleIndex].id || 0)
                    }
                    taskAnchorId = anchor
                    taskAnchorIndex = taskListView.indexAt(4, Math.max(4, taskListView.height / 3))
                    taskScrollY = Number(taskListView.contentY || 0)
                    taskLastScrollY = taskScrollY
                    taskPositionSaved = true
                }
                function restoreTaskListPosition() {
                    var anchorId = taskAnchorId
                    var anchorIndex = taskAnchorIndex
                    var hasSavedScroll = taskPositionSaved
                    var savedScrollY = hasSavedScroll ? taskScrollY : taskLastScrollY
                    if (!hasSavedScroll && !anchorId && anchorIndex < 0 && savedScrollY <= 0)
                        return
                    Qt.callLater(function() {
                        var rows = taskListView.model || []
                        tasksPage.taskRestoringPosition = true
                        if (hasSavedScroll) {
                            var maxScrollY = Math.max(0, taskListView.contentHeight - taskListView.height)
                            taskListView.contentY = Math.max(0, Math.min(savedScrollY, maxScrollY))
                        } else if (savedScrollY > 0) {
                            var maxAutoScrollY = Math.max(0, taskListView.contentHeight - taskListView.height)
                            taskListView.contentY = Math.max(0, Math.min(savedScrollY, maxAutoScrollY))
                        } else {
                            var target = -1
                            for (var i = 0; i < rows.length; i++) {
                                if (anchorId && Number(rows[i].id || 0) === anchorId) {
                                    target = i
                                    break
                                }
                            }
                            if (target < 0 && anchorIndex >= 0 && anchorIndex < rows.length)
                                target = anchorIndex
                            if (target >= 0)
                                taskListView.positionViewAtIndex(target, ListView.Contain)
                        }
                        taskAnchorId = 0
                        taskAnchorIndex = -1
                        taskScrollY = 0
                        taskPositionSaved = false
                        tasksPage.taskRestoringPosition = false
                    })
                }
                function completedTaskRows() {
                    var rows = []
                    var source = backend.taskRows || []
                    for (var i = 0; i < source.length; i++) {
                        var status = String(source[i].status || "")
                        if (status === "done" || status === "completed" || status === "no_more"
                                || (status === "incomplete" && Boolean(source[i].search_exhausted))) rows.push(source[i])
                    }
                    return rows
                }
                function completedTaskSummary() {
                    var rows = completedTaskRows()
                    var works = 0
                    var leads = 0
                    var details = []
                    for (var i = 0; i < rows.length; i++) {
                        var taskWorks = Number(rows[i].valid_video_done !== undefined ? rows[i].valid_video_done : rows[i].video_done || 0)
                        var taskLeads = Number(rows[i].lead_count || 0)
                        works += taskWorks
                        leads += taskLeads
                        details.push("#" + rows[i].id + " " + String(rows[i].keyword || "未命名任务") + "：作品 " + taskWorks + "，线索 " + taskLeads)
                    }
                    return "已完成 " + rows.length + " 个任务 · 有效作品 " + works + " · 进入线索中心 " + leads + " 条 · " + details.join("；")
                }
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 30
                    spacing: 17
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "任务中心"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                        Item { Layout.fillWidth: true }
                        ComboBox {
                            id: taskStatusFilterChooser
                            Layout.preferredWidth: 150
                            Layout.preferredHeight: 34
                            model: [
                                {label: "全部状态", value: "all"},
                                {label: "待采集", value: "pending"},
                                {label: "搜索采集中", value: "phase_a_search"},
                                {label: "评论采集中", value: "phase_b_comments"},
                                {label: "暂停", value: "paused"},
                                {label: "待继续采集", value: "incomplete"},
                                 {label: "待人工验证", value: "waiting_human"},
                                {label: "等待账号", value: "waiting_account"},
                                {label: "无可用账号", value: "no_account"},
                                {label: "已完成", value: "done"},
                                {label: "暂无更多视频", value: "no_more"},
                                {label: "失败", value: "failed"},
                                {label: "已停止", value: "stopped"}
                            ]
                            textRole: "label"
                            valueRole: "value"
                            delegate: darkComboDelegate
                            onActivated: {
                                tasksPage.taskStatusFilter = String(currentValue || "all")
                                tasksPage.taskAnchorId = 0
                                tasksPage.taskAnchorIndex = -1
                            }
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight; font.pixelSize: 11 }
                            background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                        }
                        AppButton {
                            text: "刷新"
                            onClicked: { tasksPage.rememberTaskListPosition(); backend.refresh() }
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            background: Rectangle { radius: 9; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "＋ 新建任务"
                            onClicked: taskDialog.open()
                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            background: Rectangle { radius: 9; color: window.blue }
                        }
                    }
                    Text { text: "单次采集和定时增量监控统一管理 · 状态由后台服务实时维护"; color: window.muted; font.pixelSize: 12 }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: 13
                        color: window.panel
                        border.color: window.line
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 18
                            spacing: 0
                            Rectangle {
                                visible: tasksPage.taskStatusFilter === "all" && tasksPage.completedTaskRows().length > 0
                                Layout.fillWidth: true
                                Layout.preferredHeight: 38
                                color: "#14243a"
                                radius: 8
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: 10
                                    anchors.rightMargin: 8
                                    spacing: 10
                                    AppButton {
                                        Layout.preferredWidth: 112
                                        Layout.preferredHeight: 28
                                        text: tasksPage.showCompletedTasks ? "收起已完成" : "展开已完成"
                                        onClicked: tasksPage.showCompletedTasks = !tasksPage.showCompletedTasks
                                        contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                        background: Rectangle { radius: 7; color: parent.hovered ? "#263e63" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                                    }
                                    Text { text: tasksPage.completedTaskSummary(); color: window.muted; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 11 }
                                }
                            }
                            Item { visible: tasksPage.taskStatusFilter === "all" && tasksPage.completedTaskRows().length > 0; Layout.preferredHeight: 8 }
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 38
                                spacing: 12
                                Text { text: "任务编号"; color: window.muted; Layout.preferredWidth: tasksPage.taskIdWidth; Layout.minimumWidth: tasksPage.taskIdWidth; Layout.maximumWidth: tasksPage.taskIdWidth; leftPadding: 12 }
                                Text { text: "关键词"; color: window.muted; Layout.fillWidth: true }
                                Text { text: "平台"; color: window.muted; Layout.preferredWidth: tasksPage.taskPlatformWidth; Layout.minimumWidth: tasksPage.taskPlatformWidth; Layout.maximumWidth: tasksPage.taskPlatformWidth }
                                Text { text: "采集进度"; color: window.muted; Layout.preferredWidth: tasksPage.taskProgressWidth; Layout.minimumWidth: tasksPage.taskProgressWidth; Layout.maximumWidth: tasksPage.taskProgressWidth }
                                Text { text: "状态"; color: window.muted; Layout.preferredWidth: tasksPage.taskStatusWidth; Layout.minimumWidth: tasksPage.taskStatusWidth; Layout.maximumWidth: tasksPage.taskStatusWidth }
                                Item { Layout.preferredWidth: tasksPage.taskActionsWidth; Layout.minimumWidth: tasksPage.taskActionsWidth; Layout.maximumWidth: tasksPage.taskActionsWidth }
                            }
                            Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
                            ListView {
                                id: taskListView
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                model: {
                                    var rows = backend.taskRows
                                    var filter = tasksPage.taskStatusFilter
                                    var filtered = []
                                    for (var i = 0; i < rows.length; i++) {
                                        if (filter === "all" && !tasksPage.showCompletedTasks) {
                                            var completed = rows[i].status === "done" || rows[i].status === "completed"
                                            if (completed) continue
                                        }
                                        if (tasksPage.taskMatchesStatus(rows[i], filter))
                                            filtered.push(rows[i])
                                    }
                                    return filtered
                                }
                                spacing: 1
                                onModelChanged: tasksPage.restoreTaskListPosition()
                                onMovementEnded: tasksPage.taskLastScrollY = Number(contentY || 0)
                                onContentYChanged: if (!tasksPage.taskRestoringPosition && Number(contentY || 0) > 0) tasksPage.taskLastScrollY = Number(contentY || 0)
                                delegate: Rectangle {
                                    width: ListView.view.width
                                     height: 98
                                    color: index % 2 === 0 ? "transparent" : "#17273d"
                                     RowLayout {
                                         anchors.fill: parent
                                         anchors.leftMargin: 0
                                         anchors.rightMargin: 0
                                         spacing: 12
                                         Text { text: "#" + modelData.id; color: window.muted; Layout.preferredWidth: tasksPage.taskIdWidth; Layout.minimumWidth: tasksPage.taskIdWidth; Layout.maximumWidth: tasksPage.taskIdWidth }
                                         ColumnLayout {
                                             Layout.fillWidth: true
                                             Layout.alignment: Qt.AlignVCenter
                                             spacing: 3
                                             Text { text: modelData.keyword; color: window.ink; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 12 }
                                             Text { text: modelData.keyword_count > 1 ? ("关键词进度：" + (modelData.keyword_queries || []).filter(window.isKeywordFinished).length + " / " + modelData.keyword_count) : "单关键词任务"; color: window.muted; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 10 }
                                         }
                                         ColumnLayout {
                                             Layout.preferredWidth: tasksPage.taskPlatformWidth
                                             Layout.minimumWidth: tasksPage.taskPlatformWidth
                                             Layout.maximumWidth: tasksPage.taskPlatformWidth
                                             Layout.alignment: Qt.AlignVCenter
                                             spacing: 3
                                             RowLayout { Layout.fillWidth: true; spacing: 7
                                                 Loader { Layout.preferredWidth: 22; Layout.preferredHeight: 22; sourceComponent: userPlatformIconComponent; onLoaded: item.platform = modelData.platform }
                                                 Text { text: modelData.platform_label; color: window.blue; Layout.fillWidth: true; elide: Text.ElideRight }
                                             }
                                             Text { text: "账号：" + String(modelData.account_label || "自动分配"); color: window.muted; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 10 }
                                         }
                                         RowLayout {
                                             Layout.preferredWidth: tasksPage.taskProgressWidth
                                             Layout.minimumWidth: tasksPage.taskProgressWidth
                                             Layout.maximumWidth: tasksPage.taskProgressWidth
                                             spacing: 9
                                             Item {
                                                 Layout.preferredWidth: 118
                                                 Layout.preferredHeight: 12
                                                 Layout.alignment: Qt.AlignVCenter
                                                 Rectangle {
                                                     id: taskProgressTrack
                                                     property real progressRatio: Math.min(1, Math.max(0, Number(modelData.progress || 0) / 100))
                                                     anchors.fill: parent
                                                     radius: 6
                                                     clip: true
                                                     color: "#0d1727"
                                                     Rectangle {
                                                         width: taskProgressTrack.width * taskProgressTrack.progressRatio
                                                         height: parent.height
                                                         radius: 6
                                                         clip: true
                                                         Canvas {
                                                             id: taskProgressCanvas
                                                             anchors.fill: parent
                                                             antialiasing: true
                                                             onPaint: {
                                                                 var ctx = getContext("2d")
                                                                 ctx.clearRect(0, 0, width, height)
                                                                 // 按当前已填充长度铺开渐变，低进度时也能看到完整的颜色过渡。
                                                                 var fill = ctx.createLinearGradient(0, 0, Math.max(width, 1), 0)
                                                                 fill.addColorStop(0.0, "#4676ff")
                                                                 fill.addColorStop(0.5, "#1ed4e8")
                                                                 fill.addColorStop(1.0, "#3fe38b")
                                                                 ctx.fillStyle = fill
                                                                 var radius = Math.min(6, height / 2)
                                                                 ctx.beginPath()
                                                                 ctx.moveTo(radius, 0)
                                                                 ctx.lineTo(width - radius, 0)
                                                                 ctx.quadraticCurveTo(width, 0, width, radius)
                                                                 ctx.lineTo(width, height - radius)
                                                                 ctx.quadraticCurveTo(width, height, width - radius, height)
                                                                 ctx.lineTo(radius, height)
                                                                 ctx.quadraticCurveTo(0, height, 0, height - radius)
                                                                 ctx.lineTo(0, radius)
                                                                 ctx.quadraticCurveTo(0, 0, radius, 0)
                                                                 ctx.closePath()
                                                                 ctx.fill()
                                                             }
                                                             onWidthChanged: requestPaint()
                                                             onHeightChanged: requestPaint()
                                                         }
                                                         Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
                                                     }
                                                     onProgressRatioChanged: taskProgressCanvas.requestPaint()
                                                 }
                                             }
                                             ColumnLayout { Layout.fillWidth: true; spacing: 2
                                                 Text { text: (modelData.valid_video_done !== undefined ? modelData.valid_video_done : modelData.video_done) + " / " + (modelData.effective_target_count || modelData.target_count || 0) + " 个有效作品"; color: window.ink; font.pixelSize: 11 }
                                                  Text { text: Number(modelData.comments || 0) + " 条已采集评论" + (modelData.status === "phase_b_comments" ? " · 正在读取" : "") + " · " + modelData.progress + "%"; color: window.green; font.pixelSize: 10 }
                                                 Text { text: "起：" + window.taskTimeLabel(modelData.start_at); color: window.muted; font.pixelSize: 9; elide: Text.ElideRight }
                                                 Text { text: "止：" + window.taskTimeLabel(modelData.end_at); color: window.muted; font.pixelSize: 9; elide: Text.ElideRight }
                                             }
                                         }
                                         ColumnLayout {
                                             Layout.preferredWidth: tasksPage.taskStatusWidth
                                             Layout.minimumWidth: tasksPage.taskStatusWidth
                                             Layout.maximumWidth: tasksPage.taskStatusWidth
                                             Layout.alignment: Qt.AlignVCenter
                                             spacing: 3
                                             Text { text: window.taskStatusLabel(modelData); color: modelData.status === "failed" ? window.red : window.muted; Layout.fillWidth: true; elide: Text.ElideRight }
                                             Text { visible: String(modelData.error_reason || "") !== ""; text: String(modelData.error_reason || ""); color: window.amber; Layout.fillWidth: true; maximumLineCount: 2; wrapMode: Text.WordWrap; elide: Text.ElideRight; font.pixelSize: 9 }
                                         }
                                         RowLayout {
                                             Layout.preferredWidth: tasksPage.taskActionsWidth
                                             Layout.minimumWidth: tasksPage.taskActionsWidth
                                             Layout.maximumWidth: tasksPage.taskActionsWidth
                                             spacing: 7
                                             AppButton {
                                                 Layout.preferredWidth: 58
                                                text: window.taskActionText(modelData)
                                                enabled: window.isRunningStatus(modelData.status) || window.canResumeStatus(modelData.status) || modelData.status === "pending"
                                                onClicked: {
                                                    tasksPage.rememberTaskListPosition(modelData.id)
                                                    if (window.isRunningStatus(modelData.status)) backend.pauseTask(modelData.id)
                                                    else if (window.canResumeStatus(modelData.status)) backend.resumeTask(modelData.id)
                                                    else if (modelData.status === "pending") backend.startTask(modelData.id)
                                                }
                                                contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                                                background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                            }
                                             AppButton {
                                                Layout.preferredWidth: 50
                                                text: "停止"
                                                enabled: window.isRunningStatus(modelData.status) || modelData.status === "paused"
                                                onClicked: { tasksPage.rememberTaskListPosition(modelData.id); backend.stopTask(modelData.id) }
                                                contentItem: Text { text: parent.text; color: parent.enabled ? "#f08b8b" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                                                background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                            }
                                            AppButton {
                                                Layout.preferredWidth: 50
                                                text: "导出"
                                                enabled: window.canExportTaskStatus(modelData.status)
                                                onClicked: { tasksPage.rememberTaskListPosition(modelData.id); backend.exportTask(modelData.id) }
                                                contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                                background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                            }
                                            AppButton {
                                                Layout.preferredWidth: 50
                                                text: "目录"
                                                enabled: window.canOpenTaskFolderStatus(modelData.status)
                                                onClicked: { tasksPage.rememberTaskListPosition(modelData.id); backend.openTaskFolder(modelData.id) }
                                                contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                                background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                            }
                                            AppButton {
                                                Layout.preferredWidth: 50
                                                text: tasksPage.deletingTaskId === Number(modelData.id) ? "删除中…" : "删除"
                                                enabled: tasksPage.deletingTaskId === 0
                                                onClicked: tasksPage.requestDelete(modelData)
                                                contentItem: Text { text: parent.text; color: parent.enabled ? "#ff9b9b" : "#7187a3"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                                background: Rectangle { radius: 7; color: parent.hovered && parent.enabled ? "#3a2633" : window.panel2; border.color: parent.hovered && parent.enabled ? "#e97583" : window.line }
                                            }
                                            AppButton {
                                                Layout.preferredWidth: 78
                                                text: modelData.monitoring_rule && modelData.monitoring_rule.enabled ? "监控中" : "监控设置"
                                                onClicked: { tasksPage.rememberTaskListPosition(modelData.id); monitorDialog.openFor(modelData) }
                                                contentItem: Text { text: parent.text; color: parent.enabled ? window.green : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Page {
                id: accountsPage
                background: Rectangle { color: window.color }
                property int accountNameWidth: 205
                property int accountPlatformWidth: 116
                property int accountStatusWidth: 120
                property int accountStatsWidth: 145
                property int selectedAccountId: 0
                property string selectedWindowId: ""
                property string deleteCandidateWindowId: ""
                property string deleteCandidateWindowName: ""
                // 账号页首次进入时自动补读一次昵称；后台刷新不会重复触发。
                property bool nicknameRefreshRequested: false
                property var platformOptions: [
                    {label: "全部平台", value: ""},
                    {label: "抖音", value: "douyin"},
                    {label: "小红书", value: "xhs"},
                    {label: "B站", value: "bilibili"},
                    {label: "微博", value: "weibo"},
                    {label: "快手", value: "kuaishou"},
                    {label: "百度贴吧", value: "tieba"},
                ]
                function platformFromWindow(item) {
                    var hint = (String(item.platform || "") + " " + String(item.name || "")).toLowerCase()
                    if (hint.indexOf("xiaohongshu") >= 0 || hint.indexOf("小红") >= 0) return "xhs"
                    if (hint.indexOf("douyin") >= 0 || hint.indexOf("抖音") >= 0) return "douyin"
                    if (hint.indexOf("bilibili") >= 0 || hint.indexOf("b站") >= 0 || hint.indexOf("哔哩") >= 0) return "bilibili"
                    if (hint.indexOf("weibo") >= 0 || hint.indexOf("微博") >= 0) return "weibo"
                    if (hint.indexOf("kuaishou") >= 0 || hint.indexOf("gifshow") >= 0 || hint.indexOf("快手") >= 0) return "kuaishou"
                    return ""
                }
                function platformLabel(platform) {
                    var labels = {douyin: "抖音", xhs: "小红书", bilibili: "B站", weibo: "微博", kuaishou: "快手", tieba: "百度贴吧"}
                    return labels[String(platform || "")] || "未识别平台"
                }
                function accountListRows() {
                    var result = []
                    var boundWindowIds = {}
                    for (var i = 0; i < backend.accountRows.length; i++) {
                        var account = backend.accountRows[i]
                        result.push(account)
                        var boundId = String(account.window_id || "").trim()
                        if (boundId) boundWindowIds[boundId] = true
                    }
                    // 账号快照只包含已写入数据库的账号；诊断结果中的窗口补齐
                    // 尚未绑定的 profile，确保创建后能在同一页看到并继续绑定。
                    var windows = backend.diagnosticBitBrowserWindows || []
                    for (var j = 0; j < windows.length; j++) {
                        var item = windows[j] || {}
                        var windowId = String(item.id || "").trim()
                        if (!windowId || boundWindowIds[windowId]) continue
                        var platform = accountsPage.platformFromWindow(item)
                        result.push({
                            id: 0,
                            identity: "window:" + windowId,
                            name: String(item.name || "未命名窗口"),
                            platform: platform,
                            platform_label: accountsPage.platformLabel(platform),
                            status: item.opened ? "idle" : "pending",
                            status_label: item.opened ? "已打开" : "未打开",
                            window_id: windowId,
                            binding_label: "未绑定账号",
                            processed_count: 0,
                            batch_count: 0,
                            is_window_only: true
                        })
                    }
                    return result
                }
                function filteredAccounts() {
                    var result = []
                    var platform = String(accountPlatformFilter.currentValue || "")
                    var rows = accountsPage.accountListRows()
                    for (var i = 0; i < rows.length; i++) {
                        if (!platform || rows[i].platform === platform) result.push(rows[i])
                    }
                    return result
                }
                function selectedAccountData() {
                    var rows = accountsPage.accountListRows()
                    for (var i = 0; i < rows.length; i++) {
                        if (rows[i].is_window_only && selectedWindowId && String(rows[i].window_id || "") === selectedWindowId)
                            return rows[i]
                        if (!rows[i].is_window_only && selectedAccountId > 0 && Number(rows[i].id || 0) === Number(selectedAccountId))
                            return rows[i]
                    }
                    return null
                }
                function selectedHasWindow() {
                    var account = selectedAccountData()
                    return Boolean(account && String(account.window_id || "").trim())
                }
                function requestDeleteWindow(row) {
                    if (!row || !row.is_window_only || !String(row.window_id || "").trim()) return
                    deleteCandidateWindowId = String(row.window_id || "").trim()
                    deleteCandidateWindowName = String(row.name || "未命名窗口")
                    browserWindowDeleteDialog.open()
                }
                function confirmDeleteWindow() {
                    var id = String(deleteCandidateWindowId || "").trim()
                    if (!id) return
                    browserWindowDeleteDialog.close()
                    deleteCandidateWindowId = ""
                    deleteCandidateWindowName = ""
                    selectedWindowId = ""
                    selectedAccountId = 0
                    backend.deleteBrowserWindow(id)
                }
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 30
                    spacing: 17
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "账号管理"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                        Item { Layout.fillWidth: true }
                        ComboBox {
                            id: accountPlatformFilter
                            Layout.preferredWidth: 140
                            model: accountsPage.platformOptions
                            textRole: "label"
                            valueRole: "value"
                            delegate: darkComboDelegate
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                            background: Rectangle { radius: 8; color: window.panel; border.color: window.line }
                        }
                        AppButton {
                            text: "刷新账号"
                            onClicked: {
                                backend.refresh()
                                backend.inspectBitBrowser()
                                accountsPage.nicknameRefreshRequested = true
                                backend.refreshAccountNicknames()
                            }
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            background: Rectangle { radius: 9; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "＋ 创建浏览器"
                            onClicked: createBrowserDialog.open()
                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            background: Rectangle { radius: 9; color: window.blue; border.color: window.line }
                        }
                        AppButton {
                            text: "＋ 添加账号"
                            onClicked: {
                                var selected = accountsPage.selectedAccountData()
                                if (selected && selected.is_window_only) accountDialog.openForWindow(selected)
                                else accountDialog.open()
                            }
                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            background: Rectangle { radius: 9; color: window.blue; border.color: window.line }
                        }
                    }
                    Text { text: "账号、平台和比特浏览器窗口绑定状态统一查看"; color: window.muted; font.pixelSize: 12 }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: 13
                        color: window.panel
                        border.color: window.line
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 18
                            spacing: 0
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 38
                                spacing: 12
                                Text { text: "账号"; color: window.muted; Layout.preferredWidth: accountsPage.accountNameWidth; Layout.minimumWidth: accountsPage.accountNameWidth; Layout.maximumWidth: accountsPage.accountNameWidth; leftPadding: 12 }
                                Text { text: "平台"; color: window.muted; Layout.preferredWidth: accountsPage.accountPlatformWidth; Layout.minimumWidth: accountsPage.accountPlatformWidth; Layout.maximumWidth: accountsPage.accountPlatformWidth }
                                Text { text: "状态"; color: window.muted; Layout.preferredWidth: accountsPage.accountStatusWidth; Layout.minimumWidth: accountsPage.accountStatusWidth; Layout.maximumWidth: accountsPage.accountStatusWidth }
                                Text { text: "浏览器窗口"; color: window.muted; Layout.fillWidth: true }
                                Text { text: "处理统计"; color: window.muted; Layout.preferredWidth: accountsPage.accountStatsWidth; Layout.minimumWidth: accountsPage.accountStatsWidth; Layout.maximumWidth: accountsPage.accountStatsWidth }
                            }
                            Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
                            ListView {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                model: accountsPage.filteredAccounts()
                                spacing: 1
                                delegate: Rectangle {
                                    width: ListView.view.width
                                    height: 64
                                    color: ((modelData.is_window_only && accountsPage.selectedWindowId === String(modelData.window_id || "")) || (!modelData.is_window_only && accountsPage.selectedAccountId === modelData.id)) ? "#20395c" : (index % 2 === 0 ? "transparent" : "#17273d")
                                    MouseArea { anchors.fill: parent; z: 0; onClicked: { if (modelData.is_window_only) { accountsPage.selectedAccountId = 0; accountsPage.selectedWindowId = String(modelData.window_id || "") } else { accountsPage.selectedAccountId = Number(modelData.id || 0); accountsPage.selectedWindowId = String(modelData.window_id || "") } } }
                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 0
                                        anchors.rightMargin: 0
                                        spacing: 12
                                        z: 1
                                        ColumnLayout {
                                            Layout.preferredWidth: accountsPage.accountNameWidth
                                            Layout.minimumWidth: accountsPage.accountNameWidth
                                            Layout.maximumWidth: accountsPage.accountNameWidth
                                            spacing: 2
                                            Text { text: modelData.name; color: window.ink; elide: Text.ElideRight; Layout.fillWidth: true }
                                            Text { text: modelData.is_window_only ? "待绑定浏览器窗口" : "账号 ID：" + modelData.id; color: window.muted; font.pixelSize: 11 }
                                        }
                                        RowLayout {
                                            Layout.preferredWidth: accountsPage.accountPlatformWidth
                                            Layout.minimumWidth: accountsPage.accountPlatformWidth
                                            Layout.maximumWidth: accountsPage.accountPlatformWidth
                                            spacing: 7
                                            Loader { Layout.preferredWidth: 24; Layout.preferredHeight: 24; sourceComponent: userPlatformIconComponent; onLoaded: item.platform = modelData.platform }
                                            Text { text: modelData.platform_label; color: window.blue; Layout.fillWidth: true; elide: Text.ElideRight }
                                        }
                                        Text { text: modelData.status_label; color: modelData.status === "waiting_human" ? window.amber : (modelData.is_window_only && modelData.status === "idle" ? window.green : window.muted); Layout.preferredWidth: accountsPage.accountStatusWidth; Layout.minimumWidth: accountsPage.accountStatusWidth; Layout.maximumWidth: accountsPage.accountStatusWidth; elide: Text.ElideRight }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2
                                            Text { text: modelData.binding_label; color: modelData.is_window_only ? window.amber : (modelData.window_id ? window.green : window.amber) }
                                            Text { text: modelData.window_id || "未设置窗口 ID"; color: window.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                                        }
                                        Text { text: modelData.processed_count + " 条 / " + modelData.batch_count + " 批"; color: window.muted; Layout.preferredWidth: accountsPage.accountStatsWidth; Layout.minimumWidth: accountsPage.accountStatsWidth; Layout.maximumWidth: accountsPage.accountStatsWidth; elide: Text.ElideRight }
                                    }
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 48
                                spacing: 9
                                Text { text: accountsPage.selectedAccountData() ? (accountsPage.selectedAccountData().is_window_only ? "已选浏览器窗口" : "已选账号 #" + accountsPage.selectedAccountId) : "请选择账号查看可用操作"; color: window.muted; Layout.fillWidth: true; font.pixelSize: 11; elide: Text.ElideRight }
                                AppButton {
                                    text: "打开浏览器"
                                    enabled: accountsPage.selectedHasWindow()
                                    onClicked: backend.openAccountBrowser(accountsPage.selectedAccountId)
                                    contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.enabled ? window.line : window.line }
                                }
                                AppButton {
                                    text: accountsPage.selectedAccountData() && accountsPage.selectedAccountData().is_window_only
                                          ? "添加账号"
                                          : (accountsPage.selectedAccountData() && accountsPage.selectedAccountData().nickname_resolved ? "刷新昵称" : "读取昵称")
                                    enabled: accountsPage.selectedHasWindow()
                                    onClicked: { var selected = accountsPage.selectedAccountData(); if (selected && selected.is_window_only) accountDialog.openForWindow(selected); else backend.bindAccount(accountsPage.selectedAccountId) }
                                    contentItem: Text { text: parent.text; color: parent.enabled ? window.green : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.enabled ? window.line : window.line }
                                }
                                AppButton {
                                    text: "解除绑定"
                                    enabled: accountsPage.selectedAccountId > 0
                                    onClicked: { backend.removeAccount(accountsPage.selectedAccountId); accountsPage.selectedAccountId = 0 }
                                    contentItem: Text { text: parent.text; color: parent.enabled ? "#ff9b9b" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.enabled ? "#74434d" : window.line }
                                }
                                AppButton {
                                    text: "删除窗口"
                                    enabled: { var selected = accountsPage.selectedAccountData(); return Boolean(selected && selected.is_window_only && String(selected.window_id || "").trim()) }
                                    onClicked: accountsPage.requestDeleteWindow(accountsPage.selectedAccountData())
                                    contentItem: Text { text: parent.text; color: parent.enabled ? "#ff9b9b" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.enabled ? "#74434d" : window.line }
                                }
                            }
                        }
                    }
                }
            }

            Page {
                id: leadPage
                background: Rectangle { color: window.color }
                property var selectedLeadIds: []
                property bool exportAllFiltered: false
                property var selectedLead: null
                property int currentPage: 1
                property bool commentTimeDescending: true
                property var platformOptions: [
                    {label: "全部平台", value: ""},
                    {label: "抖音", value: "douyin"},
                    {label: "小红书", value: "xhs"},
                    {label: "B站", value: "bilibili"},
                    {label: "微博", value: "weibo"},
                    {label: "快手", value: "kuaishou"},
                    {label: "百度贴吧", value: "tieba"},
                ]
                property var intentOptions: [
                    {label: "全部意向", value: ""},
                    {label: "高", value: "high"},
                    {label: "中", value: "medium"},
                    {label: "低", value: "low"},
                    {label: "未知", value: "unknown"}
                ]
                function requestLeadRefresh() {
                    currentPage = 1
                    backend.refreshLeads(
                        1,
                        String(taskFilter.currentValue || 0),
                        String(platformFilterLeads.currentValue || ""),
                        String(provinceFilter.currentText === "全部地区" ? "" : provinceFilter.currentText || ""),
                        String(intentFilter.currentValue || ""),
                        "comment_time", commentTimeDescending ? "desc" : "asc",
                        String(leadKeywordFilterField.text || "").trim()
                    )
                }
                function requestLeadPage(page) {
                    var target = Math.max(1, Math.min(page, backend.leadPages || 1))
                    currentPage = target
                    backend.refreshLeads(
                        target,
                        String(taskFilter.currentValue || 0),
                        String(platformFilterLeads.currentValue || ""),
                        String(provinceFilter.currentText === "全部地区" ? "" : provinceFilter.currentText || ""),
                        String(intentFilter.currentValue || ""),
                        "comment_time", commentTimeDescending ? "desc" : "asc",
                        String(leadKeywordFilterField.text || "").trim()
                    )
                }
                function changeLeadTask() {
                    // 任务切换后，旧任务的详情不能继续留在右侧；列表查询完成
                    // 前先清空，避免用户误以为详情属于新任务。
                    selectedLead = null
                    selectedLeadIds = []
                    exportAllFiltered = false
                    requestLeadRefresh()
                }
                function toggleLead(id) {
                    var next = selectedLeadIds.slice()
                    var index = next.indexOf(id)
                    if (index >= 0) next.splice(index, 1)
                    else next.push(id)
                    selectedLeadIds = next
                    exportAllFiltered = false
                }
                function toggleSelectAll() {
                    var ids = []
                    for (var i = 0; i < backend.leadRows.length; i++) {
                        var id = Number(backend.leadRows[i].id || 0)
                        if (id > 0) ids.push(id)
                    }
                    if (exportAllFiltered) {
                        selectedLeadIds = []
                        exportAllFiltered = false
                        return
                    }
                    selectedLeadIds = ids
                    exportAllFiltered = ids.length > 0
                }
                function selectLead(item) { selectedLead = item }
                function leadStat(key, fallback) {
                    var value = backend.leadStats[key]
                    return value === undefined || value === null ? fallback : value
                }
                Component.onCompleted: requestLeadRefresh()

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 26
                    spacing: 13
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "线索中心"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                        Text { text: "共 " + backend.leadTotal + " 条"; color: window.muted; font.pixelSize: 12; Layout.leftMargin: 8 }
                        Item { Layout.fillWidth: true }
                        AppButton {
                            text: leadPage.exportAllFiltered ? "取消全选" : "全选"
                            enabled: backend.leadRows.length > 0 || leadPage.exportAllFiltered
                            onClicked: leadPage.toggleSelectAll()
                            contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: parent.hovered && parent.enabled ? "#263e63" : window.panel2; border.color: parent.hovered && parent.enabled ? window.blue : window.line }
                        }
                        AppButton {
                            text: "导出选中"
                            enabled: leadPage.selectedLeadIds.length > 0
                            onClicked: backend.exportLeads(leadPage.selectedLeadIds)
                            contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "按任务导出"
                            enabled: leadPage.exportAllFiltered || leadPage.selectedLeadIds.length > 0
                            onClicked: backend.exportLeadsByTask(
                                leadPage.selectedLeadIds,
                                leadPage.exportAllFiltered,
                                String(taskFilter.currentValue || 0),
                                String(platformFilterLeads.currentValue || ""),
                                String(provinceFilter.currentText === "全部地区" ? "" : provinceFilter.currentText || ""),
                                String(intentFilter.currentValue || ""),
                                String(leadKeywordFilterField.text || "").trim()
                            )
                            contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: "transparent"; border.color: parent.enabled ? "#365174" : window.line }
                        }
                        AppButton {
                            text: "批量打标签"
                            enabled: leadPage.selectedLeadIds.length > 0
                            onClicked: leadTagDialog.open()
                            contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "＋ 加入互动中心"
                            enabled: leadPage.selectedLeadIds.length > 0
                            onClicked: {
                                backend.addLeadsToInteraction(leadPage.selectedLeadIds)
                                leadPage.selectedLeadIds = []
                                leadPage.selectedLead = null
                            }
                            contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: parent.enabled ? window.blue : window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "＋ 加入发私信"
                            enabled: leadPage.selectedLeadIds.length > 0
                            onClicked: {
                                backend.addLeadsToPrivateMessage(leadPage.selectedLeadIds)
                                leadPage.selectedLeadIds = []
                                leadPage.selectedLead = null
                            }
                            contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: parent.hovered && parent.enabled ? "#263e63" : window.panel2; border.color: parent.hovered && parent.enabled ? window.blue : window.line }
                        }
                    }
                    Text { text: "统一查看采集结果、识别意向并安排后续互动"; color: window.muted; font.pixelSize: 12 }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 5
                        columnSpacing: 10
                        Repeater {
                            model: [
                                {label: "今日新增线索", key: "today_new", color: "#6ea7ff", suffix: ""},
                                {label: "高意向线索", key: "high_intent", color: "#45d5a1", suffix: ""},
                                {label: "等待跟进", key: "awaiting_review", color: "#f0b75a", suffix: ""},
                                {label: "互动回复率", key: "reply_rate", color: "#a991ff", suffix: "%"},
                                {label: "需要人工处理", key: "human_required", color: "#ff8b8b", suffix: ""}
                            ]
                            delegate: Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 88
                                radius: 10
                                color: window.panel
                                border.color: window.line
                                Rectangle { anchors.right: parent.right; anchors.top: parent.top; width: 45; height: 45; radius: 23; color: modelData.color; opacity: 0.08 }
                                Column {
                                    anchors.left: parent.left; anchors.top: parent.top; anchors.leftMargin: 14; anchors.topMargin: 12; spacing: 6
                                    Text { text: modelData.label; color: window.muted; font.pixelSize: 11 }
                                    Text { text: leadPage.leadStat(modelData.key, 0) + modelData.suffix; color: modelData.color; font.pixelSize: 21; font.weight: Font.DemiBold }
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        ComboBox {
                            id: taskFilter
                            Layout.fillWidth: true
                            Layout.preferredWidth: 330
                            model: backend.leadTaskOptions
                            textRole: "label"
                            valueRole: "id"
                            delegate: darkComboDelegate
                            onActivated: leadPage.changeLeadTask()
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 12; elide: Text.ElideRight }
                            background: Rectangle { radius: 8; color: window.panel; border.color: window.line }
                        }
                        ComboBox {
                            id: platformFilterLeads
                            Layout.preferredWidth: 135
                            model: leadPage.platformOptions
                            textRole: "label"
                            valueRole: "value"
                            delegate: darkComboDelegate
                            onActivated: leadPage.requestLeadRefresh()
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 12 }
                            background: Rectangle { radius: 8; color: window.panel; border.color: window.line }
                        }
                        ComboBox {
                            id: provinceFilter
                            Layout.preferredWidth: 135
                            model: backend.leadProvinceOptions
                            delegate: darkComboDelegate
                            onActivated: leadPage.requestLeadRefresh()
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 12 }
                            background: Rectangle { radius: 8; color: window.panel; border.color: window.line }
                        }
                        ComboBox {
                            id: intentFilter
                            Layout.preferredWidth: 120
                            model: leadPage.intentOptions
                            textRole: "label"
                            valueRole: "value"
                            delegate: darkComboDelegate
                            onActivated: leadPage.requestLeadRefresh()
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 12 }
                            background: Rectangle { radius: 8; color: window.panel; border.color: window.line }
                        }
                        TextField {
                            id: leadKeywordFilterField
                            Layout.preferredWidth: 220
                            Layout.minimumWidth: 160
                            placeholderText: "关键词筛选：昵称、评论、词组"
                            color: window.ink
                            palette.placeholderText: window.muted
                            selectByMouse: true
                            onAccepted: leadPage.requestLeadRefresh()
                            background: Rectangle { radius: 8; color: window.panel; border.color: parent.activeFocus ? window.blue : window.line }
                        }
                        AppButton {
                            text: "筛选"
                            onClicked: leadPage.requestLeadRefresh()
                            contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: parent.hovered ? "#263e63" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                        }
                        Item { Layout.fillWidth: true }
                        AppButton {
                            text: "刷新"
                            onClicked: leadPage.requestLeadRefresh()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "关键词管理"
                            onClicked: keywordGroupDialog.openForManage()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: parent.hovered ? "#263e63" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 12
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            radius: 12
                            color: window.panel
                            border.color: window.line
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 14
                                spacing: 0
                                RowLayout {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 38
                                    Layout.leftMargin: 8
                                    Layout.rightMargin: 8
                                    spacing: 9
                                    Text { text: "选择"; color: window.muted; Layout.preferredWidth: 54; leftPadding: 8 }
                                    Text { text: "用户"; color: window.muted; Layout.preferredWidth: 160 }
                                    Text { text: "评论原文"; color: window.muted; Layout.fillWidth: true }
                                     AppButton {
                                         Layout.preferredWidth: 145
                                         Layout.minimumWidth: 145
                                         Layout.maximumWidth: 145
                                         text: "评论时间 " + (leadPage.commentTimeDescending ? "↓" : "↑")
                                         onClicked: { leadPage.commentTimeDescending = !leadPage.commentTimeDescending; leadPage.requestLeadRefresh() }
                                         contentItem: Text { text: parent.text; color: window.blue; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                         background: Rectangle { color: "transparent" }
                                     }
                                    Text { text: "原作地址"; color: window.muted; Layout.preferredWidth: 82 }
                                }
                                Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
                                ListView {
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    clip: true
                                    spacing: 1
                                    model: backend.leadRows
                                    delegate: Rectangle {
                                        width: ListView.view.width
                                        height: 70
                                        color: index % 2 === 0 ? "transparent" : "#17273d"
                                        MouseArea { anchors.fill: parent; z: 0; onClicked: leadPage.selectLead(modelData) }
                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 8
                                            anchors.rightMargin: 8
                                            spacing: 9
                                            z: 1
                                            Rectangle {
                                                Layout.preferredWidth: 54
                                                Layout.preferredHeight: 42
                                                color: "transparent"
                                                Rectangle { anchors.centerIn: parent; width: 18; height: 18; radius: 4; color: leadPage.selectedLeadIds.indexOf(modelData.id) >= 0 ? window.blue : "transparent"; border.color: leadPage.selectedLeadIds.indexOf(modelData.id) >= 0 ? window.blue : "#7187a3"; Text { anchors.centerIn: parent; text: "✓"; color: "#071224"; visible: leadPage.selectedLeadIds.indexOf(modelData.id) >= 0; font.pixelSize: 13 } }
                                                MouseArea { anchors.fill: parent; onClicked: { leadPage.toggleLead(modelData.id); leadPage.selectLead(modelData) } }
                                            }
                                             ColumnLayout {
                                                 Layout.preferredWidth: 160; spacing: 3
                                                 RowLayout { Layout.fillWidth: true; spacing: 6
                                                     Loader { Layout.preferredWidth: 20; Layout.preferredHeight: 20; sourceComponent: userPlatformIconComponent; onLoaded: item.platform = modelData.platform }
                                                     Text { text: modelData.nickname; color: window.ink; Layout.fillWidth: true; elide: Text.ElideRight }
                                                 }
                                                 Text { text: modelData.platform_label + " · " + modelData.region_label; color: window.muted; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                                            }
                                            Text { text: modelData.comment; color: window.ink; Layout.fillWidth: true; Layout.alignment: Qt.AlignVCenter; maximumLineCount: 2; wrapMode: Text.WordWrap; elide: Text.ElideRight }
                                            Text { text: modelData.comment_time; color: window.muted; Layout.preferredWidth: 145; elide: Text.ElideRight; font.pixelSize: 11 }
                                            AppButton {
                                                Layout.preferredWidth: 82
                                                text: modelData.source_url_label
                                                enabled: Boolean(modelData.source_url)
                                                onClicked: backend.openSourceUrl(modelData.source_url, modelData.comment || modelData.nickname || "")
                                                contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                                background: Rectangle { radius: 7; color: "transparent"; border.color: parent.enabled ? "#365174" : window.line }
                                            }
                                        }
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 42
                                    Text { text: "已选 " + leadPage.selectedLeadIds.length + " 条"; color: window.muted; font.pixelSize: 11 }
                                    Item { Layout.fillWidth: true }
                                    AppButton {
                                        text: "上一页"
                                        enabled: backend.leadPage > 1
                                        onClicked: leadPage.requestLeadPage(backend.leadPage - 1)
                                        contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                        background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                    }
                                    Text { text: backend.leadPages ? backend.leadPage + " / " + backend.leadPages : "暂无数据"; color: window.muted; font.pixelSize: 11 }
                                    AppButton {
                                        text: "下一页"
                                        enabled: backend.leadPage < backend.leadPages
                                        onClicked: leadPage.requestLeadPage(backend.leadPage + 1)
                                        contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                        background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                    }
                                }
                            }
                        }
                        Rectangle {
                            Layout.preferredWidth: 360
                            Layout.minimumWidth: 350
                            Layout.maximumWidth: 410
                            Layout.fillHeight: true
                            radius: 12
                            color: "#101b2d"
                            border.color: window.line
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 16
                                spacing: 11
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { text: "线索详情"; color: window.ink; font.pixelSize: 16; font.weight: Font.Medium }
                                    Item { Layout.fillWidth: true }
                                    AppButton { text: "原作 ↗"; visible: leadPage.selectedLead !== null && Boolean(leadPage.selectedLead.source_url); onClicked: backend.openSourceUrl(leadPage.selectedLead.source_url, leadPage.selectedLead.comment || leadPage.selectedLead.nickname || ""); contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 6; color: parent.hovered ? window.panel2 : "transparent" } }
                                }
                                Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
                                ColumnLayout {
                                    visible: leadPage.selectedLead !== null
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    spacing: 12
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 48
                                        Rectangle { Layout.preferredWidth: 38; Layout.preferredHeight: 38; radius: 19; color: "#4b6ee8"; Text { anchors.centerIn: parent; text: leadPage.selectedLead ? leadPage.selectedLead.nickname.slice(0, 1) : ""; color: "#ffffff"; font.pixelSize: 16 } }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 3
                                            Text { text: leadPage.selectedLead ? leadPage.selectedLead.nickname : ""; color: window.ink; font.pixelSize: 13; elide: Text.ElideRight; Layout.fillWidth: true }
                                            RowLayout { Layout.fillWidth: true; spacing: 6
                                                Loader { property string platformValue: leadPage.selectedLead ? leadPage.selectedLead.platform : ""; Layout.preferredWidth: 20; Layout.preferredHeight: 20; sourceComponent: userPlatformIconComponent; onLoaded: item.platform = platformValue; onPlatformValueChanged: if (item) item.platform = platformValue }
                                                Text { text: leadPage.selectedLead ? leadPage.selectedLead.platform_label + " · " + leadPage.selectedLead.region_label : ""; color: window.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                                            }
                                        }
                                    }
                                    Rectangle {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 72
                                        Layout.minimumHeight: 72
                                        radius: 9
                                        color: window.panel
                                        border.color: window.line
                                        ColumnLayout {
                                            anchors.fill: parent
                                            anchors.margins: 12
                                            spacing: 6
                                            RowLayout {
                                                Layout.fillWidth: true
                                                Text { text: leadPage.selectedLead ? leadPage.selectedLead.intent_label + "意向" : ""; color: window.green; font.pixelSize: 18; font.weight: Font.DemiBold }
                                                Item { Layout.fillWidth: true }
                                                Text { text: leadPage.selectedLead ? leadPage.selectedLead.status_label : ""; color: window.blue; font.pixelSize: 11; elide: Text.ElideRight }
                                            }
                                            Text { text: "人工筛选后可加入互动中心"; color: window.muted; font.pixelSize: 10; Layout.fillWidth: true; elide: Text.ElideRight }
                                        }
                                    }
                                    Text { text: "评论原文"; color: window.muted; font.pixelSize: 11; Layout.preferredHeight: 16 }
                                    Rectangle {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 106
                                        Layout.minimumHeight: 88
                                        radius: 8
                                        color: "#17273d"
                                        border.color: window.line
                                        Text { anchors.fill: parent; anchors.margins: 10; text: leadPage.selectedLead ? leadPage.selectedLead.comment : ""; color: window.ink; font.pixelSize: 12; wrapMode: Text.WordWrap; maximumLineCount: 5; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                                    }
                                    ColumnLayout { Layout.fillWidth: true; spacing: 3; Layout.preferredHeight: 35
                                        Text { text: "评论时间"; color: window.muted; font.pixelSize: 10 }
                                        Text { text: leadPage.selectedLead ? leadPage.selectedLead.comment_time : ""; color: window.ink; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                                    }
                                    ColumnLayout { Layout.fillWidth: true; spacing: 3; Layout.preferredHeight: 35
                                        Text { text: "地区"; color: window.muted; font.pixelSize: 10 }
                                        Text { text: leadPage.selectedLead ? leadPage.selectedLead.region_label : "未识别"; color: window.ink; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                                    }
                                    Item { Layout.fillHeight: true }
                                }
                                ColumnLayout {
                                    visible: leadPage.selectedLead === null; Layout.fillWidth: true; Layout.fillHeight: true
                                    Item { Layout.fillHeight: true }
                                    Text { text: "选择一条线索"; color: window.muted; font.pixelSize: 13; Layout.alignment: Qt.AlignHCenter }
                                    Text { text: "点击左侧列表查看完整评论和详情"; color: "#60748f"; font.pixelSize: 11; Layout.alignment: Qt.AlignHCenter }
                                    Item { Layout.fillHeight: true }
                                }
                                AppButton {
                                    Layout.fillWidth: true
                                    enabled: leadPage.selectedLead !== null
                                    text: "加入互动中心"
                                    onClicked: {
                                        backend.addLeadsToInteraction([leadPage.selectedLead.id])
                                        leadPage.selectedLeadIds = []
                                        leadPage.selectedLead = null
                                    }
                                    contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                                    background: Rectangle { radius: 8; color: parent.enabled ? window.blue : window.panel2; border.color: window.line }
                                }
                                AppButton {
                                    Layout.fillWidth: true
                                    enabled: leadPage.selectedLead !== null
                                    text: "加入发私信"
                                    onClicked: {
                                        backend.addLeadsToPrivateMessage([leadPage.selectedLead.id])
                                        leadPage.selectedLeadIds = []
                                        leadPage.selectedLead = null
                                    }
                                    contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                                    background: Rectangle { radius: 8; color: parent.hovered && parent.enabled ? "#263e63" : window.panel2; border.color: parent.hovered && parent.enabled ? window.blue : window.line }
                                }
                            }
                        }
                    }
                }
            }
            Page {
                id: interactionPage2
                background: Rectangle { color: window.color }
                property string activeType: "comment_reply"
                property string activeStatus: "draft"
                property var selectedDraftIds: []
                property var pendingActionDraftIds: []
                property var pendingSendDraftIds: []
                property var pendingTypeSwitchLeadIds: []
                property bool realSendEnabled: false
                property bool realSendConfirmed: false
                property var statusOptions: [
                    {key: "draft", label: "待生成"},
                    {key: "queued", label: "待发送"},
                    {key: "sent", label: "已回复"},
                    {key: "failed", label: "失败"}
                ]
                property var platformOptions: [
                    {label: "全部平台", value: ""},
                    {label: "抖音", value: "douyin"},
                    {label: "小红书", value: "xhs"},
                    {label: "B站", value: "bilibili"},
                    {label: "微博", value: "weibo"},
                    {label: "快手", value: "kuaishou"},
                    {label: "百度贴吧", value: "tieba"},
                ]
                function requestInteractionRefresh() {
                    backend.refreshInteractions(
                        1,
                        activeStatus,
                        String(platformFilter.currentValue || ""),
                        String(accountFilter.currentValue || 0),
                        activeType
                    )
                }
                function requestInteractionPage(page) {
                    backend.refreshInteractions(
                        Math.max(1, Number(page || 1)),
                        activeStatus,
                        String(platformFilter.currentValue || ""),
                        String(accountFilter.currentValue || 0),
                        activeType
                    )
                }
                property var interactionTypeOptions: [
                    {key: "comment_reply", label: "评论回复"},
                    {key: "private_message", label: "发私信"}
                ]
                function accountOptionsFor(platform) {
                    var options = [{id: 0, label: "选择账号"}]
                    for (var i = 0; i < backend.interactionAccounts.length; i++) {
                        var account = backend.interactionAccounts[i]
                        if (!platform || account.platform === platform)
                            options.push(account)
                    }
                    return options
                }
                function bulkAccountOptions() {
                    var selectedPlatform = ""
                    for (var i = 0; i < selectedDraftIds.length; i++) {
                        for (var j = 0; j < backend.interactionRows.length; j++) {
                            var row = backend.interactionRows[j]
                            if (row.draft_id === selectedDraftIds[i]) {
                                if (!selectedPlatform) selectedPlatform = row.platform
                                else if (selectedPlatform !== row.platform) return [{id: 0, label: "请只选择同一平台"}]
                                break
                            }
                        }
                    }
                    return accountOptionsFor(selectedPlatform)
                }
                function effectiveAccountId(row) {
                    var selected = selectedDraftIds.indexOf(Number(row.draft_id)) >= 0
                    var bulkId = Number(bulkAccountFilter.currentValue || 0)
                    if (activeStatus === "queued" && selected && bulkId > 0)
                        return bulkId
                    return Number(row.reply_account_id || 0)
                }
                function applyBulkAccount(accountId) {
                    var selectedAccountId = Number(accountId || 0)
                    if (selectedAccountId <= 0 || activeStatus !== "queued") return
                    var ids = selectedDraftIds.slice()
                    for (var i = 0; i < ids.length; i++)
                        dispatchAction(ids[i], "assign_account", selectedAccountId, "", "")
                }
                function toggleDraft(id) {
                    var next = selectedDraftIds.slice()
                    var index = next.indexOf(id)
                    if (index >= 0) next.splice(index, 1)
                    else next.push(id)
                    selectedDraftIds = next
                }
                function isActionPending(id) {
                    return pendingActionDraftIds.indexOf(Number(id)) >= 0
                }
                function dispatchAction(id, action, accountId, content, templateId) {
                    var draftId = Number(id || 0)
                    if (draftId <= 0 || isActionPending(draftId)) return
                    var next = pendingActionDraftIds.slice()
                    next.push(draftId)
                    pendingActionDraftIds = next
                    backend.interactionAction(
                        draftId, String(action || ""), Number(accountId || 0),
                        String(content || ""), String(templateId || "")
                    )
                }
                function finishAction(id) {
                    var draftId = Number(id || 0)
                    if (draftId <= 0) return
                    pendingActionDraftIds = pendingActionDraftIds.filter(function(item) {
                        return Number(item) !== draftId
                    })
                }
                function isSendPending(id) {
                    return pendingSendDraftIds.indexOf(Number(id)) >= 0
                }
                function sendDrafts(ids, accountId) {
                    var normalized = []
                    for (var i = 0; i < (ids || []).length; i++) {
                        var draftId = Number(ids[i] || 0)
                        if (draftId > 0 && normalized.indexOf(draftId) < 0)
                            normalized.push(draftId)
                    }
                    if (!normalized.length || Number(accountId || 0) <= 0)
                        return
                    for (var j = 0; j < normalized.length; j++)
                        if (isSendPending(normalized[j])) return
                    pendingSendDraftIds = normalized
                    backend.sendInteractions(
                        normalized, Number(accountId),
                        interactionPage2.realSendEnabled,
                        interactionPage2.realSendConfirmed
                    )
                }
                function selectStatus(status) {
                    activeStatus = status
                    selectedDraftIds = []
                    pendingActionDraftIds = []
                    pendingSendDraftIds = []
                    requestInteractionRefresh()
                }
                function selectInteractionType(kind) {
                    activeType = String(kind || "comment_reply")
                    selectedDraftIds = []
                    pendingActionDraftIds = []
                    pendingSendDraftIds = []
                    requestInteractionRefresh()
                }
                function switchRowType(row) {
                    var leadId = Number(row && row.lead_id || 0)
                    if (leadId <= 0 || pendingTypeSwitchLeadIds.indexOf(leadId) >= 0) return
                    var target = activeType === "private_message" ? "comment_reply" : "private_message"
                    var next = pendingTypeSwitchLeadIds.slice()
                    next.push(leadId)
                    pendingTypeSwitchLeadIds = next
                    backend.switchInteractionType(leadId, target)
                }
                function finishTypeSwitch(leadId) {
                    var id = Number(leadId || 0)
                    pendingTypeSwitchLeadIds = pendingTypeSwitchLeadIds.filter(function(item) {
                        return Number(item) !== id
                    })
                }
                Component.onCompleted: requestInteractionRefresh()

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 30
                    spacing: 14
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "互动中心"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                        Text { text: "共 " + backend.interactionTotal + " 条"; color: window.muted; font.pixelSize: 12; Layout.leftMargin: 8 }
                        Item { Layout.fillWidth: true }
                        AppButton {
                            text: "按任务导出"
                            onClicked: backend.exportInteractionsByTask("", "", "")
                            contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 9; color: "transparent"; border.color: parent.hovered ? window.blue : "#365174" }
                        }
                        CheckBox {
                            id: realSendSwitch
                            text: "真实发送"
                            checked: interactionPage2.realSendEnabled
                            onToggled: {
                                if (checked && !interactionPage2.realSendEnabled) {
                                    checked = false
                                    realSendConfirmDialog.open()
                                } else if (!checked) {
                                    interactionPage2.realSendEnabled = false
                                    interactionPage2.realSendConfirmed = false
                                }
                            }
                            spacing: 8
                            // 给复选框指示器预留空间，避免文字从 x=0 开始
                            // 与前面的勾选框重叠。
                            leftPadding: 24
                            Layout.preferredWidth: 104
                            Layout.minimumWidth: 104
                            Layout.maximumWidth: 104
                            contentItem: Text { text: realSendSwitch.text; color: realSendSwitch.checked ? "#ff9696" : window.muted; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            indicator: Rectangle {
                                implicitWidth: 16; implicitHeight: 16
                                x: 0; y: (realSendSwitch.height - height) / 2
                                radius: 4
                                color: realSendSwitch.checked ? "#e97583" : "transparent"
                                border.color: realSendSwitch.checked ? "#e97583" : "#7187a3"
                                Text { anchors.centerIn: parent; text: "✓"; visible: realSendSwitch.checked; color: "#ffffff"; font.pixelSize: 11 }
                            }
                        }
                        AppButton {
                            text: "回复模板"
                            onClicked: templateDialog.open()
                            contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 9; color: "transparent"; border.color: "#365174" }
                        }
                        AppButton {
                            text: "刷新"
                            onClicked: interactionPage2.requestInteractionRefresh()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            background: Rectangle { radius: 9; color: window.panel2; border.color: window.line }
                        }
                    }
                    Text {
                        text: interactionPage2.realSendEnabled ? "警告：真实发送已开启，点击发送后会执行平台最终发送动作" : (interactionPage2.activeType === "private_message" ? "完整私信内容与人工审核 · 当前仅填入，不点击发送" : "完整回复内容与人工审核 · 当前仅填入，不点击发送")
                        color: interactionPage2.realSendEnabled ? "#ff9696" : window.muted
                        font.pixelSize: 12
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Text { text: "互动方式"; color: window.muted; font.pixelSize: 12 }
                        Repeater {
                            model: interactionPage2.interactionTypeOptions
                            delegate: AppButton {
                                Layout.preferredWidth: 112
                                Layout.preferredHeight: 32
                                text: modelData.label
                                onClicked: interactionPage2.selectInteractionType(modelData.key)
                                contentItem: Text { text: parent.text; color: interactionPage2.activeType === modelData.key ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                                background: Rectangle { radius: 8; color: interactionPage2.activeType === modelData.key ? "#1b477b" : window.panel2; border.color: interactionPage2.activeType === modelData.key ? "#4776aa" : window.line }
                            }
                        }
                        Text { text: interactionPage2.activeType === "private_message" ? "按用户主页定位，不依赖来源评论" : "按来源作品定位评论并回复"; color: window.muted; font.pixelSize: 11; Layout.leftMargin: 5 }
                        Item { Layout.fillWidth: true }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Repeater {
                            model: interactionPage2.statusOptions
                            delegate: AppButton {
                                Layout.preferredWidth: 108
                                Layout.preferredHeight: 36
                                text: modelData.label
                                onClicked: interactionPage2.selectStatus(modelData.key)
                                contentItem: Text { text: parent.text; color: interactionPage2.activeStatus === modelData.key ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                                background: Rectangle { radius: 9; color: interactionPage2.activeStatus === modelData.key ? "#1b477b" : window.panel2; border.color: interactionPage2.activeStatus === modelData.key ? "#4776aa" : window.line }
                            }
                        }
                        Item { Layout.fillWidth: true }
                        ComboBox {
                            id: platformFilter
                            visible: interactionPage2.activeStatus === "sent"
                            Layout.preferredWidth: 138
                            model: interactionPage2.platformOptions
                            textRole: "label"
                            valueRole: "value"
                            delegate: darkComboDelegate
                            onActivated: interactionPage2.requestInteractionRefresh()
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                            background: Rectangle { radius: 8; color: window.panel; border.color: window.line }
                        }
                        ComboBox {
                            id: accountFilter
                            visible: interactionPage2.activeStatus === "sent"
                            Layout.preferredWidth: 190
                            model: [{id: 0, label: "全部账号"}].concat(backend.interactionAccounts)
                            textRole: "label"
                            valueRole: "id"
                            delegate: darkComboDelegate
                            onActivated: interactionPage2.requestInteractionRefresh()
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 10; elide: Text.ElideRight }
                            background: Rectangle { radius: 8; color: window.panel; border.color: window.line }
                        }
                    }
                    RowLayout {
                        visible: interactionPage2.activeStatus === "queued"
                        Layout.fillWidth: true
                        spacing: 9
                        Text { text: "待发送多选：" + interactionPage2.selectedDraftIds.length + " 条"; color: window.muted; font.pixelSize: 11 }
                        ComboBox {
                            id: bulkAccountFilter
                            Layout.preferredWidth: 230
                            model: interactionPage2.bulkAccountOptions()
                            textRole: "label"
                            valueRole: "id"
                            delegate: darkComboDelegate
                            onActivated: interactionPage2.applyBulkAccount(currentValue)
                            contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 10; elide: Text.ElideRight }
                            background: Rectangle { radius: 8; color: window.panel; border.color: window.line }
                        }
                        AppButton {
                            text: interactionPage2.realSendEnabled ? "一键发送" : "一键模拟回复"
                            enabled: interactionPage2.selectedDraftIds.length > 0 && bulkAccountFilter.currentValue > 0
                            onClicked: {
                                interactionPage2.sendDrafts(interactionPage2.selectedDraftIds, bulkAccountFilter.currentValue)
                                interactionPage2.selectedDraftIds = []
                            }
                            contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
                            background: Rectangle { radius: 8; color: parent.enabled ? window.blue : window.panel2; border.color: window.line }
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: 13
                        color: window.panel
                        border.color: window.line
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 16
                            spacing: 0
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 38
                                Text { text: interactionPage2.activeStatus === "queued" ? "选择" : "平台"; color: window.muted; Layout.preferredWidth: 64; Layout.minimumWidth: 64; leftPadding: 0; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                Text { text: interactionPage2.activeType === "private_message" ? "用户与私信目标" : "用户与原评论"; color: window.muted; Layout.preferredWidth: 260; Layout.minimumWidth: 220; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                Text { text: interactionPage2.activeType === "private_message" ? "完整私信内容" : "完整回复话术"; color: window.muted; Layout.fillWidth: true; Layout.minimumWidth: 220; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                Text { text: "账号 / 操作"; color: window.muted; Layout.preferredWidth: 300; Layout.minimumWidth: 250; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            }
                            Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
                            ListView {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                spacing: 1
                                // 切换页签后，后台新查询返回前不要继续显示上一个
                                // 状态的旧数据，避免“已回复”短暂显示待发送内容。
                                model: String(backend.interactionStatus || "draft") === interactionPage2.activeStatus
                                       && String(backend.interactionType || "comment_reply") === interactionPage2.activeType
                                       ? backend.interactionRows : []
                                delegate: Rectangle {
                                    width: ListView.view.width
                                    property real textBlockHeight: Math.max(
                                        originalText.implicitHeight,
                                        replyEditor.visible ? replyEditor.implicitHeight : replyText.implicitHeight
                                    )
                                    height: Math.max(138, Math.min(230, textBlockHeight + 94))
                                    color: index % 2 === 0 ? "transparent" : "#17273d"
                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 8
                                        anchors.rightMargin: 8
                                        spacing: 12
                                        Item {
                                            Layout.preferredWidth: 64
                                            Layout.minimumWidth: 64
                                            Layout.fillHeight: true
                                             Loader {
                                                 anchors.top: parent.top
                                                 anchors.topMargin: 10
                                                 anchors.left: parent.left
                                                 anchors.leftMargin: 10
                                                 width: 24; height: 24
                                                 sourceComponent: userPlatformIconComponent
                                                 onLoaded: item.platform = modelData.platform
                                             }
                                             Text { anchors.top: parent.top; anchors.topMargin: 36; anchors.horizontalCenter: parent.horizontalCenter; width: 64; text: modelData.platform_label; color: window.blue; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight; font.pixelSize: 9 }
                                            Rectangle {
                                                visible: interactionPage2.activeStatus === "queued"
                                                         && String(modelData.status || "") === "queued"
                                                anchors.top: parent.top
                                                anchors.topMargin: 48
                                                anchors.left: parent.left
                                                width: 18; height: 18; radius: 4
                                                color: interactionPage2.selectedDraftIds.indexOf(modelData.draft_id) >= 0 ? window.blue : "transparent"
                                                border.color: interactionPage2.selectedDraftIds.indexOf(modelData.draft_id) >= 0 ? window.blue : "#7187a3"
                                                Text { anchors.centerIn: parent; text: "✓"; color: "#071224"; visible: interactionPage2.selectedDraftIds.indexOf(modelData.draft_id) >= 0; font.pixelSize: 13 }
                                                MouseArea { anchors.fill: parent; onClicked: interactionPage2.toggleDraft(modelData.draft_id) }
                                            }
                                        }
                                        ColumnLayout {
                                            Layout.preferredWidth: 260
                                            Layout.minimumWidth: 220
                                            Layout.maximumWidth: 300
                                            Layout.alignment: Qt.AlignTop
                                            Layout.topMargin: 12
                                            spacing: 5
                                            Text { text: modelData.nickname; color: window.ink; Layout.fillWidth: true; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight; horizontalAlignment: interactionPage2.activeStatus === "draft" ? Text.AlignLeft : Text.AlignHCenter; font.pixelSize: 13 }
                                            Text { text: interactionPage2.activeType === "private_message" ? (modelData.platform_user_id ? "用户ID：" + modelData.platform_user_id : "私信目标用户") : (modelData.task_id > 0 ? "任务#" + modelData.task_id : "未关联任务"); color: window.blue; Layout.fillWidth: true; elide: Text.ElideRight; horizontalAlignment: interactionPage2.activeStatus === "draft" ? Text.AlignLeft : Text.AlignHCenter; font.pixelSize: 10 }
                                            Text { text: modelData.comment_time; color: window.muted; Layout.fillWidth: true; elide: Text.ElideRight; horizontalAlignment: interactionPage2.activeStatus === "draft" ? Text.AlignLeft : Text.AlignHCenter; font.pixelSize: 10 }
                                            Text { id: originalText; text: interactionPage2.activeType === "private_message" ? (modelData.profile_url ? "个人主页：" + modelData.profile_url : "未记录个人主页地址") : modelData.original_comment; color: "#c8d7ea"; Layout.fillWidth: true; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight; horizontalAlignment: interactionPage2.activeStatus === "draft" ? Text.AlignLeft : Text.AlignHCenter; font.pixelSize: 12 }
                                        }
                                         ColumnLayout {
                                             Layout.fillWidth: true
                                             Layout.minimumWidth: 220
                                             Layout.alignment: Qt.AlignTop
                                             Layout.topMargin: 10
                                             spacing: 5
                                             Text { visible: interactionPage2.activeStatus === "draft"; text: interactionPage2.activeType === "private_message" ? "私信内容（可编辑）" : "回复内容（可编辑）"; color: window.muted; font.pixelSize: 10 }
                                             TextArea {
                                                 id: replyEditor
                                                 visible: interactionPage2.activeStatus === "draft"
                                                 Layout.fillWidth: true
                                                 Layout.preferredHeight: 76
                                                 text: modelData.content
                                                 color: window.ink
                                                 wrapMode: TextArea.Wrap
                                                 selectByMouse: true
                                                 font.pixelSize: 12
                                                 background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                             }
                                             Text { id: replyText; visible: interactionPage2.activeStatus !== "draft"; text: modelData.content; color: window.ink; Layout.fillWidth: true; wrapMode: Text.WordWrap; maximumLineCount: 3; elide: Text.ElideRight; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 12 }
                                             RowLayout {
                                                 visible: interactionPage2.activeStatus === "draft"
                                                 Layout.fillWidth: true
                                                 spacing: 6
                                                 ComboBox {
                                                     id: templateChooser
                                                     Layout.fillWidth: true
                                                      model: backend.interactionTemplates
                                                      textRole: "content"
                                                      valueRole: "id"
                                                      delegate: darkComboDelegate
                                                     currentIndex: {
                                                         for (var i = 0; i < count; i++) if (model[i].id === modelData.template_id) return i
                                                         return count > 0 ? 0 : -1
                                                     }
                                                     contentItem: Text { text: parent.displayText; color: window.muted; verticalAlignment: Text.AlignVCenter; leftPadding: 8; elide: Text.ElideRight; font.pixelSize: 10 }
                                                     background: Rectangle { radius: 7; color: window.panel; border.color: window.line }
                                                 }
                                                 AppButton {
                                                     text: "套用"
                                                     enabled: templateChooser.currentValue !== undefined && templateChooser.currentValue !== ""
                                                     onClicked: interactionPage2.dispatchAction(modelData.draft_id, "apply_template", 0, "", String(templateChooser.currentValue || ""))
                                                     contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                     background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
                                                 }
                                                 AppButton {
                                                     text: "保存"
                                                     enabled: replyEditor.text.trim().length > 0
                                                     onClicked: interactionPage2.dispatchAction(modelData.draft_id, "update_content", 0, replyEditor.text, "")
                                                     contentItem: Text { text: parent.text; color: parent.enabled ? window.green : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                     background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
                                                 }
                                             }
                                         }
                                         ColumnLayout {
                                             Layout.preferredWidth: 300
                                            Layout.minimumWidth: 250
                                            Layout.maximumWidth: 340
                                            Layout.alignment: Qt.AlignTop
                                            Layout.topMargin: 10
                                            spacing: 7
                                            Text { visible: interactionPage2.activeStatus === "failed"; text: "失败原因：" + (modelData.failure_reason || "未记录"); color: "#ff9b9b"; Layout.fillWidth: true; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 11 }
                                            Text { visible: interactionPage2.activeStatus === "queued" && String(modelData.status || "") === "sending"; text: "浏览器发送中，请等待结果…"; color: "#f5c66a"; Layout.fillWidth: true; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 11 }
                                            Text { visible: interactionPage2.activeStatus === "sent"; text: "回复账号：" + modelData.reply_account_name; color: window.muted; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight; font.pixelSize: 11 }
                                            Text { visible: interactionPage2.activeStatus === "sent"; text: "回复状态：" + (modelData.reply_status_label || "已回复") + " · 客户回复：" + (modelData.customer_replied_label || "否"); color: window.green; Layout.fillWidth: true; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 10 }
                                            ComboBox {
                                                visible: interactionPage2.activeStatus === "queued"
                                                         && String(modelData.status || "") === "queued"
                                                enabled: !interactionPage2.isActionPending(modelData.draft_id)
                                                Layout.fillWidth: true
                                                 model: interactionPage2.accountOptionsFor(modelData.platform)
                                                 textRole: "label"
                                                 valueRole: "id"
                                                 delegate: darkComboDelegate
                                                currentIndex: {
                                                    var effectiveId = interactionPage2.effectiveAccountId(modelData)
                                                    for (var i = 0; i < count; i++) if (model[i].id === effectiveId) return i
                                                    return 0
                                                }
                                                onActivated: {
                                                    if (Number(currentValue || 0) > 0)
                                                        interactionPage2.dispatchAction(modelData.draft_id, "assign_account", currentValue, "", "")
                                                }
                                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter; leftPadding: 4; rightPadding: 4; elide: Text.ElideRight; font.pixelSize: 11 }
                                                background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                            }
                                            Flow {
                                                // 已回复记录也允许继续为同一用户创建另一种互动方式，
                                                // 因此不能把整组单条操作在 sent 状态下隐藏。
                                                visible: true
                                                Layout.fillWidth: true
                                                Layout.preferredHeight: 64
                                                spacing: 6
                                                AppButton {
                                                    width: 112
                                                    height: 30
                                                    enabled: Number(modelData.lead_id || 0) > 0
                                                             && interactionPage2.pendingTypeSwitchLeadIds.indexOf(Number(modelData.lead_id || 0)) < 0
                                                    text: interactionPage2.pendingTypeSwitchLeadIds.indexOf(Number(modelData.lead_id || 0)) >= 0
                                                          ? "处理中…"
                                                          : (interactionPage2.activeType === "private_message" ? "转到评论回复" : "转到私信")
                                                    onClicked: interactionPage2.switchRowType(modelData)
                                                    contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                    background: Rectangle { radius: 7; color: parent.hovered && parent.enabled ? "#263e63" : "transparent"; border.color: parent.enabled ? "#365174" : window.line }
                                                }
                                                AppButton {
                                                    visible: interactionPage2.activeStatus === "draft"
                                                    width: 92
                                                    height: 30
                                                    enabled: !interactionPage2.isActionPending(modelData.draft_id)
                                                    text: "进入发送页"
                                                    // QmlBridge 注册的是五参数槽函数，必须显式传入
                                                    // 空模板 ID；否则点击不会匹配到后台方法。
                                                    // 必须提交当前编辑框内容，不能重新读取初始的
                                                    // modelData.content，否则人工修改会被默认话术覆盖。
                                                    onClicked: interactionPage2.dispatchAction(modelData.draft_id, "enter_send", 0, replyEditor.text, "")
                                                    contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                    background: Rectangle { radius: 7; color: "transparent"; border.color: "#365174" }
                                                }
                                                AppButton {
                                                    visible: interactionPage2.activeStatus === "queued"
                                                    width: 56
                                                    height: 30
                                                    text: String(modelData.status || "") === "sending" ? "发送中…" : (interactionPage2.realSendEnabled ? (interactionPage2.activeType === "private_message" ? "发送私信" : "发送") : (interactionPage2.activeType === "private_message" ? "模拟私信" : "模拟"))
                                                     enabled: String(modelData.status || "") === "queued"
                                                              && interactionPage2.effectiveAccountId(modelData) > 0 && !interactionPage2.isSendPending(modelData.draft_id)
                                                     onClicked: interactionPage2.sendDrafts([modelData.draft_id], interactionPage2.effectiveAccountId(modelData))
                                                    contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                    background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
                                                }
                                                AppButton {
                                                    visible: interactionPage2.activeStatus === "queued"
                                                             && String(modelData.status || "") === "queued"
                                                    width: 92
                                                    height: 30
                                                    enabled: !interactionPage2.isActionPending(modelData.draft_id)
                                                             && !interactionPage2.isSendPending(modelData.draft_id)
                                                    text: "退回待生成"
                                                    onClicked: interactionPage2.dispatchAction(modelData.draft_id, "return_queued", 0, "", "")
                                                    contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                    background: Rectangle { radius: 7; color: "transparent"; border.color: "#365174" }
                                                }
                                                AppButton {
                                                    visible: interactionPage2.activeStatus === "failed"
                                                    width: 92
                                                    height: 30
                                                    enabled: !interactionPage2.isActionPending(modelData.draft_id)
                                                    text: "退回待生成"
                                                    onClicked: interactionPage2.dispatchAction(modelData.draft_id, "return_failed", 0, "", "")
                                                    contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                    background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
                                                }
                                                AppButton {
                                                    visible: (interactionPage2.activeStatus === "draft"
                                                              || interactionPage2.activeStatus === "failed"
                                                              || (interactionPage2.activeStatus === "queued"
                                                                  && String(modelData.status || "") === "queued"))
                                                    width: 52
                                                    height: 30
                                                    enabled: !interactionPage2.isActionPending(modelData.draft_id)
                                                    text: "删除"
                                                    onClicked: interactionPage2.dispatchAction(modelData.draft_id, "delete", 0, "", "")
                                                    contentItem: Text { text: parent.text; color: "#ff9b9b"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                    background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                Text { text: interactionPage2.activeType === "private_message" ? "当前页显示完整私信正文和私信目标" : "当前页显示完整回复正文和原评论"; color: window.muted; font.pixelSize: 11 }
                                Item { Layout.fillWidth: true }
                                AppButton {
                                    text: "上一页"
                                    enabled: backend.interactionPage > 1
                                    onClicked: interactionPage2.requestInteractionPage(backend.interactionPage - 1)
                                    contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                }
                                Text { text: backend.interactionPages ? backend.interactionPage + " / " + backend.interactionPages : "暂无数据"; color: window.muted; font.pixelSize: 11 }
                                AppButton {
                                    text: "下一页"
                                    enabled: backend.interactionPage < backend.interactionPages
                                    onClicked: interactionPage2.requestInteractionPage(backend.interactionPage + 1)
                                    contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                }
                            }
                        }
                    }
                }
            }
            Page {
                id: publishPage
                background: Rectangle { color: window.color }
                property string activeStatus: "all"
                property string activePlatform: ""
                property string searchText: ""
                property int selectedDraftId: 0
                property string selectedPlatform: "douyin"
                property string editorTitle: ""
                property string editorBody: ""
                property string editorTopics: ""
                property string selectedAccountId: "0"
                property bool saveDraftOnly: true
                property bool realPublishEnabled: false
                property string publishTimeMode: "now"
                property string scheduledAt: ""
                property string scheduleDate: ""
                property string scheduleHour: ""
                property string scheduleMinute: ""
                property string scheduleNotice: ""
                property string publishTab: "publish"
                property string accountInfoPlatform: "douyin"
                property string accountInfoAccountId: "0"
                property string accountInfoType: ""
                property int accountInfoAutoSyncGeneration: 0
                property string accountInfoLastAutoSyncKey: ""
                property string messagePlatform: ""
                property string messageAccountId: "0"
                property bool messageUnreadOnly: false
                property string messageType: ""
                property string selectedMessageGroupKey: ""
                property string messageActionNotice: ""
                property int messageActionPendingId: 0
                property string generationNotice: ""
                property bool generationPending: false
                readonly property var platformTabs: [
                    {value: "douyin", label: "抖音"},
                    {value: "xhs", label: "小红书"},
                    {value: "bilibili", label: "B站"},
                    {value: "weibo", label: "微博"}
                ]
                readonly property var accountInfoPlatformOptions: [
                    {value: "douyin", label: "抖音"},
                    {value: "xhs", label: "小红书"},
                    {value: "bilibili", label: "B站"},
                    {value: "weibo", label: "微博"},
                    {value: "kuaishou", label: "快手"}
                ]
                readonly property var platformFilterOptions: [
                    {value: "", label: "全部平台"},
                    {value: "douyin", label: "抖音"},
                    {value: "xhs", label: "小红书"},
                    {value: "bilibili", label: "B站"},
                    {value: "weibo", label: "微博"}
                ]
                readonly property var messageTypeFilterOptions: [{value: "", label: "全部类型"}].concat(backend.publishMessageTypeOptions || [])

                function requestRefresh() {
                    backend.refreshPublishDrafts(1, activeStatus, activePlatform, searchText)
                }
                function selectTab(value) {
                    publishTab = String(value || "publish")
                    if (publishTab === "account") {
                        ensureAccountInfoSelection(true)
                    } else if (publishTab === "generation") {
                        backend.refreshGeneratedContents()
                    } else if (publishTab === "messages") {
                        messageActionNotice = ""
                        ensureMessageGroupSelection()
                        backend.refreshPublishedMessages(1, messagePlatform, messageAccountId, messageUnreadOnly, messageType)
                    } else if (publishTab === "settings") {
                        backend.refreshDiagnostics()
                    } else {
                        requestRefresh()
                    }
                }
                function accountOptionsFor(platform) {
                    var result = []
                    var rows = backend.accountRows || []
                    for (var i = 0; i < rows.length; i++) {
                        var row = rows[i] || {}
                        if (String(row.platform || "") !== String(platform || "")) continue
                        result.push({value: String(row.id || "0"), label: String(row.name || "未命名账号")})
                    }
                    if (!result.length) result.push({value: "0", label: "暂无该平台账号"})
                    return result
                }
                function ensureAccountInfoSelection(sync) {
                    var options = accountOptionsFor(accountInfoPlatform)
                    var selected = Number(accountInfoAccountId || 0)
                    var selectedStillAvailable = false
                    for (var i = 0; i < options.length; i++) {
                        if (Number(options[i].value || 0) === selected && selected > 0) {
                            selectedStillAvailable = true
                            break
                        }
                    }
                    if (!selectedStillAvailable) {
                        selected = 0
                        for (var j = 0; j < options.length; j++) {
                            if (Number(options[j].value || 0) > 0) {
                                selected = Number(options[j].value)
                                break
                            }
                        }
                        accountInfoAccountId = String(selected)
                    }
                    var syncKey = accountInfoPlatform + ":" + String(selected)
                    if (sync && selected > 0 && syncKey !== accountInfoLastAutoSyncKey) {
                        accountInfoLastAutoSyncKey = syncKey
                        var generation = ++accountInfoAutoSyncGeneration
                        Qt.callLater(function() {
                            if (generation === accountInfoAutoSyncGeneration
                                && publishTab === "account"
                                && Number(accountInfoAccountId || 0) === selected) {
                                backend.syncAccountContents(selected, accountInfoPlatform)
                            }
                        })
                    }
                    return selected > 0
                }
                function allAccountOptions() {
                    var result = [{value: "0", label: "全部账号"}]
                    var rows = backend.accountRows || []
                    for (var i = 0; i < rows.length; i++) {
                        var row = rows[i] || {}
                        result.push({value: String(row.id || "0"), label: String(row.name || "未命名账号") + " · " + platformLabel(row.platform)})
                    }
                    return result
                }
                function messageAccountOptions() {
                    var platform = String(messagePlatform || "")
                    if (!platform) return allAccountOptions()
                    var result = [{value: "0", label: "全部账号"}]
                    var rows = backend.accountRows || []
                    for (var i = 0; i < rows.length; i++) {
                        var row = rows[i] || {}
                        if (String(row.platform || "") !== platform) continue
                        result.push({value: String(row.id || "0"), label: String(row.name || "未命名账号")})
                    }
                    return result
                }
                function resetMessageAccountForPlatform() {
                    var selected = String(messageAccountId || "0")
                    if (selected === "0") return
                    var options = messageAccountOptions()
                    for (var i = 0; i < options.length; i++) {
                        if (String(options[i].value || "0") === selected) return
                    }
                    messageAccountId = "0"
                }
                function messageSyncStatusText() {
                    var status = String(backend.publishMessageSyncStatus || "")
                    var summary = backend.publishMessageSyncSummary || {}
                    var errors = summary.errors || []
                    if (status === "failed") {
                        return errors.length ? ("同步失败 " + errors.length + " 项") : "同步失败"
                    }
                    if (status === "success") {
                        var processed = Number(summary.processed || 0)
                        return errors.length ? ("已同步 " + processed + " 条，失败 " + errors.length + " 项") : ("已同步 " + processed + " 条")
                    }
                    return "消息来自本地缓存"
                }
                function ensureMessageGroupSelection() {
                    var groups = backend.publishMessageGroups || []
                    if (!groups.length) {
                        selectedMessageGroupKey = ""
                        return
                    }
                    for (var i = 0; i < groups.length; i++) {
                        if (String(groups[i].key || "") === selectedMessageGroupKey)
                            return
                    }
                    selectedMessageGroupKey = String(groups[0].key || "")
                }
                function selectMessageGroup(key) {
                    selectedMessageGroupKey = String(key || "")
                    ensureMessageGroupSelection()
                }
                function selectedMessageGroup() {
                    var groups = backend.publishMessageGroups || []
                    for (var i = 0; i < groups.length; i++) {
                        if (String(groups[i].key || "") === selectedMessageGroupKey)
                            return groups[i]
                    }
                    return groups.length ? groups[0] : {}
                }
                function selectedMessageGroupRows() {
                    var group = selectedMessageGroup()
                    return group.rows || []
                }
                function platformLabel(value) {
                    var key = String(value || "")
                    if (key === "douyin") return "抖音"
                    if (key === "xhs") return "小红书"
                    if (key === "bilibili") return "B站"
                    if (key === "weibo") return "微博"
                    if (key === "kuaishou") return "快手"
                    if (key === "tieba") return "百度贴吧"
                    return key || "未知平台"
                }
                function statusColor(value) {
                    var key = String(value || "")
                    if (key === "published") return window.green
                    if (key === "queued" || key === "approved") return window.blue
                    if (key === "failed" || key === "retryable_failed") return "#ee7182"
                    return window.amber
                }
                function rowPlatforms(row) {
                    var candidates = []
                    if (row && row.platforms && row.platforms.length !== undefined)
                        candidates = row.platforms
                    if (!candidates.length && row && row.variants && row.variants.length !== undefined) {
                        for (var v = 0; v < row.variants.length; v++)
                            candidates.push(row.variants[v] && row.variants[v].platform)
                    }
                    var result = []
                    // 固定平台顺序整理，确保“全部平台”时五个平台都能显示，
                    // 不受数据库版本或插入顺序影响。
                    for (var i = 0; i < platformTabs.length; i++) {
                        var wanted = String(platformTabs[i].value)
                        for (var j = 0; j < candidates.length; j++) {
                            if (String(candidates[j] || "") === wanted && result.indexOf(wanted) < 0) {
                                result.push(wanted)
                                break
                            }
                        }
                    }
                    return result
                }
                function rowPlatformLabels(row) {
                    var values = rowPlatforms(row)
                    var labels = []
                    for (var i = 0; i < values.length; i++) labels.push(platformLabel(values[i]))
                    return labels
                }
                function firstPlatform(row) {
                    var values = rowPlatforms(row)
                    return values.length > 0 ? values[0] : "douyin"
                }
                function displayTime(value) {
                    var text = String(value || "")
                    return text.length > 16 ? text.slice(0, 16).replace("T", " ") : text
                }
                function statusCount(status) {
                    var rows = backend.publishDraftRows || []
                    var total = 0
                    for (var i = 0; i < rows.length; i++) {
                        if (String(rows[i].status || "") === status) total++
                    }
                    return total
                }
                function failedCount() {
                    return statusCount("failed") + statusCount("retryable_failed") + statusCount("human_required")
                }
                function accountOptions() {
                    var result = []
                    var rows = backend.accountRows || []
                    for (var i = 0; i < rows.length; i++) {
                        var row = rows[i] || {}
                        if (String(row.platform || "") !== selectedPlatform) continue
                        result.push({
                            value: String(row.id || "0"),
                            label: String(row.name || "未命名账号") + " · " + platformLabel(row.platform)
                        })
                    }
                    if (!result.length) result.push({value: "0", label: "暂无可用账号"})
                    return result
                }
                function selectedAccountIsValid() {
                    var selected = Number(selectedAccountId || 0)
                    if (selected <= 0) return false
                    var options = accountOptions()
                    for (var i = 0; i < options.length; i++) {
                        if (Number(options[i].value || 0) === selected) return true
                    }
                    return false
                }
                function padSchedulePart(value) {
                    var number = Number(value || 0)
                    return number < 10 ? "0" + number : String(number)
                }
                function scheduleDateParts(value) {
                    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""))
                    if (!match) return null
                    return {year: Number(match[1]), month: Number(match[2]), day: Number(match[3])}
                }
                function beijingClockParts() {
                    var raw = String(backend.beijingNowText || "")
                    var match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(raw)
                    if (!match) return {year: 2000, month: 1, day: 1, hour: 0, minute: 0, second: 0}
                    return {
                        year: Number(match[1]), month: Number(match[2]), day: Number(match[3]),
                        hour: Number(match[4]), minute: Number(match[5]), second: Number(match[6])
                    }
                }
                function scheduleTodayValue() {
                    var now = beijingClockParts()
                    return now.year + "-" + padSchedulePart(now.month) + "-" + padSchedulePart(now.day)
                }
                function scheduleDateOptions() {
                    var now = beijingClockParts()
                    var base = Date.UTC(now.year, now.month - 1, now.day)
                    var result = []
                    // 日历仍限制在今天到未来一年，后台会再次校验北京时间。
                    for (var offset = 0; offset <= 365; offset++) {
                        var date = new Date(base + offset * 24 * 60 * 60 * 1000)
                        var value = date.getUTCFullYear() + "-" + padSchedulePart(date.getUTCMonth() + 1)
                                   + "-" + padSchedulePart(date.getUTCDate())
                        result.push({value: value, label: date.getUTCFullYear() + "年"
                                     + padSchedulePart(date.getUTCMonth() + 1) + "月"
                                     + padSchedulePart(date.getUTCDate()) + "日"})
                    }
                    return result
                }
                function scheduleHourOptionsFor(dateValue) {
                    var now = beijingClockParts()
                    var today = scheduleTodayValue()
                    var result = []
                    for (var hour = 0; hour < 24; hour++) {
                        if (String(dateValue || "") === today) {
                            if (hour < now.hour) continue
                            // 当前小时只在仍有未来分钟时显示。
                            if (hour === now.hour && now.minute >= 59) continue
                        }
                        result.push({value: padSchedulePart(hour), label: padSchedulePart(hour) + "时"})
                    }
                    return result
                }
                function scheduleHourOptions() {
                    return scheduleHourOptionsFor(scheduleDate)
                }
                function scheduleMinuteOptionsFor(dateValue, hourValue) {
                    var now = beijingClockParts()
                    var today = scheduleTodayValue()
                    var hour = Number(hourValue || 0)
                    var result = []
                    for (var minute = 0; minute < 60; minute++) {
                        // 当前分钟可能只剩几秒，保守地从下一分钟开始，避免刚选完就过期。
                        if (String(dateValue || "") === today && hour === now.hour
                            && minute <= now.minute) continue
                        result.push({value: padSchedulePart(minute), label: padSchedulePart(minute) + "分"})
                    }
                    return result
                }
                function scheduleMinuteOptions() {
                    return scheduleMinuteOptionsFor(scheduleDate, scheduleHour)
                }
                function scheduleOptionIndex(options, value) {
                    for (var i = 0; i < options.length; i++)
                        if (String(options[i].value || "") === String(value || "")) return i
                    return -1
                }
                function ensureSchedulePickerSelection() {
                    var dates = scheduleDateOptions()
                    if (scheduleOptionIndex(dates, scheduleDate) < 0)
                        scheduleDate = dates.length ? String(dates[0].value) : ""
                    var hours = scheduleHourOptions()
                    if (scheduleOptionIndex(hours, scheduleHour) < 0)
                        scheduleHour = hours.length ? String(hours[0].value) : ""
                    var minutes = scheduleMinuteOptions()
                    if (scheduleOptionIndex(minutes, scheduleMinute) < 0)
                        scheduleMinute = minutes.length ? String(minutes[0].value) : ""
                    scheduledAt = buildScheduledAt()
                }
                function ensureScheduleMinuteSelection() {
                    var minutes = scheduleMinuteOptions()
                    if (scheduleOptionIndex(minutes, scheduleMinute) < 0)
                        scheduleMinute = minutes.length ? String(minutes[0].value) : ""
                    scheduledAt = buildScheduledAt()
                }
                function scheduleDisplayText() {
                    var parts = scheduleDateParts(scheduleDate)
                    if (!parts || !scheduleHour || !scheduleMinute) return "点击选择日期和时间"
                    return parts.year + "年" + padSchedulePart(parts.month) + "月"
                        + padSchedulePart(parts.day) + "日 " + scheduleHour + ":" + scheduleMinute
                }
                function openSchedulePicker() {
                    ensureSchedulePickerSelection()
                    schedulePickerDialog.openFor()
                }
                function buildScheduledAt() {
                    if (!scheduleDate || !scheduleHour || !scheduleMinute) return ""
                    return String(scheduleDate) + " " + String(scheduleHour) + ":" + String(scheduleMinute)
                }
                function scheduleSelectionIsFuture() {
                    var selected = buildScheduledAt()
                    var now = String(backend.beijingNowText || "").slice(0, 16)
                    return Boolean(selected) && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(selected)
                        && selected > now
                }
                function ensurePublishAccountSelection() {
                    if (!selectedAccountIsValid()) selectedAccountId = "0"
                    return Number(selectedAccountId || 0) > 0
                }
                function resetPublishAccountForPlatform() {
                    // 平台版本共用一个编辑区，但发布账号必须按平台重新选择。
                    // 清掉旧平台的 ID，避免“界面显示微博、请求却带B站账号”。
                    selectedAccountId = "0"
                    Qt.callLater(function() {
                        if (publishAccountChooser) publishAccountChooser.currentIndex = 0
                    })
                }
                function selectedAccountName() {
                    var id = Number(selectedAccountId || 0)
                    if (id <= 0) return "待选择账号"
                    var rows = backend.accountRows || []
                    for (var i = 0; i < rows.length; i++) {
                        if (Number(rows[i].id || 0) === id
                            && String(rows[i].platform || "") === selectedPlatform)
                            return String(rows[i].name || "未命名账号")
                    }
                    return "待选择账号"
                }
                function assetRows() {
                    var row = selectedRow()
                    var values = row && row.assets ? row.assets : []
                    if (values.length > 0) return values
                    var count = Number(row && row.asset_count || 0)
                    var placeholders = []
                    for (var i = 0; i < count; i++) placeholders.push({id: i + 1, path: "", asset_type: "image"})
                    return placeholders
                }
                function assetName(asset, index) {
                    var value = String((asset || {}).path || "")
                    if (!value) return "素材 " + (index + 1)
                    var parts = value.replace(/\\/g, "/").split("/")
                    return parts[parts.length - 1] || ("素材 " + (index + 1))
                }
                function localAssetUrl(asset) {
                    var value = String((asset || {}).path || "").trim()
                    if (!value) return ""
                    if (value.indexOf("file:") === 0) return value
                    return "file:///" + value.replace(/\\/g, "/").replace(/^\/+/, "")
                }
                function previewAsset() {
                    var values = assetRows()
                    return values.length > 0 ? (values[0] || {}) : {}
                }
                function previewAssetUrl() {
                    return localAssetUrl(previewAsset())
                }
                function previewAssetIsVideo() {
                    var asset = previewAsset()
                    var path = String(asset.path || "").toLowerCase()
                    return String(asset.asset_type || "").toLowerCase() === "video"
                        || /\.(mp4|mov|mkv|avi|webm|flv|wmv|m4v|mpeg|mpg)$/.test(path)
                }
                function realPublishAllowed() {
                    var status = String((selectedRow() || {}).status || "")
                    return selectedDraftId > 0 && selectedAccountIsValid()
                        && ["approved", "queued"].indexOf(status) >= 0
                }
                function realPublishDisabledReason() {
                    if (selectedDraftId <= 0) return "请先从左侧选择一条内容"
                    if (!selectedAccountIsValid()) return "请先选择与平台匹配的发布账号"
                    var status = String((selectedRow() || {}).status || "")
                    if (status === "published") return "当前内容已发布，不能重复发布"
                    if (status === "draft" || status === "review") return "请先提交审核并批准，再进行真实发布"
                    if (status === "cancelled") return "当前内容已取消，请先恢复到可发布状态"
                    return "当前状态暂不允许真实发布"
                }
                function loadDraft(row) {
                    if (!row) return
                    selectedDraftId = Number(row.id || 0)
                    selectedPlatform = firstPlatform(row)
                    var variants = row.variants || []
                    if (variants.length > 0) {
                        var found = null
                        for (var i = 0; i < variants.length; i++) {
                            if (String(variants[i].platform || "") === selectedPlatform) {
                                found = variants[i]
                                break
                            }
                        }
                        if (!found) {
                            found = variants[0]
                            selectedPlatform = String(found.platform || "douyin")
                        }
                        editorTitle = String(found.title || row.title || "")
                        editorBody = String(found.body || row.source_content || "")
                        editorTopics = (found.topics || []).join(", ")
                    } else {
                        editorTitle = String(row.title || "")
                        editorBody = String(row.source_content || "")
                        editorTopics = ""
                    }
                    selectedAccountId = "0"
                }
                function loadPlatformVariant() {
                    var rows = backend.publishDraftRows || []
                    for (var i = 0; i < rows.length; i++) {
                        if (Number(rows[i].id || 0) !== selectedDraftId) continue
                        var variants = rows[i].variants || []
                        for (var j = 0; j < variants.length; j++) {
                            if (String(variants[j].platform || "") === selectedPlatform) {
                                editorTitle = String(variants[j].title || "")
                                editorBody = String(variants[j].body || "")
                                editorTopics = (variants[j].topics || []).join(", ")
                                return
                            }
                        }
                        editorTitle = String(rows[i].title || "")
                        editorBody = String(rows[i].source_content || "")
                        editorTopics = ""
                        return
                    }
                }
                function selectedRow() {
                    var rows = backend.publishDraftRows || []
                    for (var i = 0; i < rows.length; i++)
                        if (Number(rows[i].id || 0) === selectedDraftId) return rows[i]
                    return null
                }
                function statusAction(row) {
                    if (!row) return ""
                    if (row.status === "draft") return "submit_review"
                    if (row.status === "review") return "approve"
                    if (row.status === "approved") return "cancel"
                    if (row.status === "cancelled") return "restore"
                    return ""
                }
                Component.onCompleted: {
                    ensureSchedulePickerSelection()
                    requestRefresh()
                }
                Connections {
                    target: backend
                    function onBeijingNowTextChanged() {
                        if (publishPage.publishTimeMode === "scheduled")
                            publishPage.ensureSchedulePickerSelection()
                    }
                    function onViewChanged() {
                        // 账号列表异步返回后自动选中该平台第一个账号，让同步
                        // 按钮首次进入页面就可用，不要求用户重复选择。
                        if (publishPage.publishTab === "account")
                            publishPage.ensureAccountInfoSelection(true)
                        else if (publishPage.publishTab === "messages")
                            publishPage.ensureMessageGroupSelection()
                    }
                }

                // 发布工作区的五个子页面保持在同一个 Page 内，只切换可见内容，
                // 不销毁重建控件，避免切换时出现整页刷新和底部向上闪动。
                ColumnLayout {
                    visible: publishPage.publishTab === "account"
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 12
                    RowLayout {
                        Layout.fillWidth: true; Layout.preferredHeight: 48
                        ColumnLayout { Layout.fillWidth: true; spacing: 2
                            Text { text: "账号信息"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                            Text { text: "按平台查看账号主页同步的视频、图文和评论详情"; color: window.muted; font.pixelSize: 12 }
                            Text {
                                text: backend.publishAccountContentSyncStatus === "success"
                                      ? "读取来源：账号主页（只读） · 已同步 " + backend.publishAccountContentTotal + " 条"
                                      : (backend.publishAccountContentSyncError !== ""
                                         ? "读取提示：" + backend.publishAccountContentSyncError
                                         : "选择账号后点击“同步账号作品”，读取该账号主页内容")
                                color: backend.publishAccountContentSyncStatus === "success" ? window.green : window.muted
                                font.pixelSize: 10
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                        AppButton { text: "同步账号作品"; Layout.preferredWidth: 116; Layout.preferredHeight: 34
                            enabled: Number(publishPage.accountInfoAccountId || 0) > 0
                            onClicked: backend.syncAccountContents(Number(publishPage.accountInfoAccountId), publishPage.accountInfoPlatform)
                            contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: parent.enabled ? window.blue : window.panel2; border.color: window.line }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 54; radius: 9; color: window.panel; border.color: window.line
                        RowLayout { anchors.fill: parent; anchors.margins: 10; spacing: 9
                            Text { text: "平台"; color: window.muted; font.pixelSize: 11 }
                            ComboBox { id: accountInfoPlatformChooser; Layout.preferredWidth: 130; Layout.preferredHeight: 32
                                model: publishPage.accountInfoPlatformOptions; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                currentIndex: {
                                    for (var i = 0; i < model.length; i++)
                                        if (String(model[i].value) === publishPage.accountInfoPlatform) return i
                                    return 0
                                }
                                onActivated: { publishPage.accountInfoPlatform = String(currentValue || "douyin"); publishPage.accountInfoAccountId = "0"; publishPage.ensureAccountInfoSelection(true) }
                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                            }
                            Text { text: "账号"; color: window.muted; font.pixelSize: 11; Layout.leftMargin: 10 }
                            ComboBox { id: accountInfoAccountChooser; Layout.fillWidth: true; Layout.preferredHeight: 32
                                model: publishPage.accountOptionsFor(publishPage.accountInfoPlatform); textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                currentIndex: {
                                    for (var i = 0; i < model.length; i++)
                                        if (String(model[i].value) === publishPage.accountInfoAccountId) return i
                                    return 0
                                }
                                onModelChanged: publishPage.ensureAccountInfoSelection(false)
                                onActivated: { publishPage.accountInfoAccountId = String(currentValue || "0"); publishPage.ensureAccountInfoSelection(true) }
                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                            }
                            Text { text: "类型"; color: window.muted; font.pixelSize: 11; Layout.leftMargin: 10 }
                            ComboBox { id: accountInfoTypeChooser; Layout.preferredWidth: 120; Layout.preferredHeight: 32
                                model: [{value: "", label: "全部内容"}, {value: "video", label: "视频"}, {value: "image", label: "图文"}]; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                onActivated: { publishPage.accountInfoType = String(currentValue || ""); backend.refreshAccountContents(Number(publishPage.accountInfoAccountId), publishPage.accountInfoPlatform, publishPage.accountInfoType) }
                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                            }
                        }
                    }
                    RowLayout { Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12
                        Rectangle { Layout.fillWidth: true; Layout.fillHeight: true; radius: 10; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 12; spacing: 8
                                RowLayout { Layout.fillWidth: true
                                    Text { text: "作品与图文"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium }
                                    Text { text: backend.publishAccountContentTotal + " 条"; color: window.muted; font.pixelSize: 10; Layout.leftMargin: 7 }
                                    Item { Layout.fillWidth: true }
                                    AppButton { text: "刷新"; Layout.preferredWidth: 62; Layout.preferredHeight: 28; onClicked: backend.refreshAccountContents(Number(publishPage.accountInfoAccountId), publishPage.accountInfoPlatform, publishPage.accountInfoType)
                                        contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                        background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                                    }
                                }
                                ListView { id: accountContentList; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 7; model: backend.publishAccountContentRows
                                    delegate: Rectangle { width: accountContentList.width; height: 74; radius: 8; color: index % 2 === 0 ? "#172840" : "#132238"; border.color: window.line
                                        RowLayout { anchors.fill: parent; anchors.margins: 10; spacing: 10
                                            Rectangle { Layout.preferredWidth: 54; Layout.preferredHeight: 54; radius: 7; color: "#203a48"; Loader { anchors.centerIn: parent; width: 24; height: 24; sourceComponent: platformIconComponent; onLoaded: item.platform = modelData.platform } }
                                            ColumnLayout { Layout.fillWidth: true; spacing: 4
                                                RowLayout { Layout.fillWidth: true
                                                    Text { text: modelData.title || "未命名内容"; color: window.ink; font.pixelSize: 12; font.weight: Font.Medium; elide: Text.ElideRight; Layout.fillWidth: true }
                                                    Text { text: modelData.content_type_label || "内容"; color: window.blue; font.pixelSize: 10 }
                                                }
                                                Text { text: (modelData.published_at || "暂无发布时间") + "  ·  点赞 " + (modelData.like_count || 0) + "  ·  评论 " + (modelData.comment_count || 0); color: window.muted; font.pixelSize: 10 }
                                            }
                                            AppButton { text: "评论详情"; Layout.preferredWidth: 72; Layout.preferredHeight: 28; onClicked: backend.refreshContentComments(Number(modelData.id))
                                                contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                background: Rectangle { radius: 7; color: "transparent"; border.color: parent.hovered ? window.blue : window.line }
                                            }
                                            AppButton { text: "打开原文"; Layout.preferredWidth: 72; Layout.preferredHeight: 28; enabled: String(modelData.url || "") !== ""; onClicked: backend.openSourceUrl(String(modelData.url || ""))
                                                contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                                            }
                                        }
                                    }
                                }
                                Text { visible: accountContentList.count === 0; text: Number(publishPage.accountInfoAccountId || 0) > 0 ? "暂无该账号主页同步的作品" : "请选择平台和账号"; color: window.muted; font.pixelSize: 11; Layout.alignment: Qt.AlignHCenter }
                            }
                        }
                        Rectangle { Layout.preferredWidth: 330; Layout.fillHeight: true; radius: 10; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 12; spacing: 8
                                Text { text: "评论区内容"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium }
                                Text { text: "选中作品后加载评论；保留原文和楼中楼标识"; color: window.muted; font.pixelSize: 10; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                ListView { id: accountCommentList; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 6; model: backend.publishAccountContentComments
                                    delegate: Rectangle { width: accountCommentList.width; height: 62; radius: 7; color: index % 2 === 0 ? "#172840" : "#132238"
                                        ColumnLayout { anchors.fill: parent; anchors.margins: 8; spacing: 3
                                            Text { text: (modelData.nickname || "匿名用户") + (Number(modelData.is_reply || 0) ? " · 楼中楼" : ""); color: window.ink; font.pixelSize: 10; Layout.fillWidth: true; elide: Text.ElideRight }
                                            Text { text: modelData.content || "暂无评论原文"; color: "#c8d7ea"; font.pixelSize: 10; Layout.fillWidth: true; maximumLineCount: 2; elide: Text.ElideRight }
                                        }
                                    }
                                }
                                Text { visible: accountCommentList.count === 0; text: "选择作品查看评论"; color: window.muted; font.pixelSize: 11; Layout.alignment: Qt.AlignHCenter }
                            }
                        }
                    }
                }

                ColumnLayout {
                    visible: publishPage.publishTab === "generation"
                    anchors.fill: parent; anchors.margins: 22; spacing: 12
                    RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 48
                        ColumnLayout { Layout.fillWidth: true; spacing: 2
                            Text { text: "内容生成"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                            Text { text: "参考选题生成系统，先生成候选内容，再一键导入发布草稿"; color: window.muted; font.pixelSize: 12 }
                        }
                        Rectangle { Layout.preferredWidth: 138; Layout.preferredHeight: 30; radius: 15; color: "#203a48"; border.color: "#2e5362"; Text { anchors.centerIn: parent; text: backend.diagnosticLlmApi.enabled && backend.diagnosticLlmApi.configured ? "智能 API 已启用" : "本地模板兜底"; color: window.green; font.pixelSize: 10 } }
                        Text { visible: publishPage.generationNotice.length > 0; text: publishPage.generationNotice; color: publishPage.generationNotice.indexOf("未返回") >= 0 ? window.amber : window.green; font.pixelSize: 10; elide: Text.ElideRight; Layout.preferredWidth: 230 }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 82; radius: 10; color: window.panel; border.color: window.line
                        ColumnLayout { anchors.fill: parent; anchors.margins: 12; spacing: 7
                            RowLayout { Layout.fillWidth: true
                                Text { text: "生成条件"; color: window.ink; font.pixelSize: 12; font.weight: Font.Medium }
                                Text { text: backend.diagnosticLlmApi.enabled && backend.diagnosticLlmApi.configured ? "当前优先调用智能 API，失败自动切换本地模板" : "未配置智能 API 时自动使用本地模板"; color: window.muted; font.pixelSize: 10; Layout.leftMargin: 8 }
                            }
                            RowLayout { Layout.fillWidth: true; spacing: 8
                                TextField { id: generationKeyword; Layout.fillWidth: true; Layout.preferredHeight: 34; placeholderText: "输入选题或关键词，例如：互联网洗衣"; placeholderTextColor: "#7187a3"; color: window.ink; leftPadding: 10; background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line } }
                                ComboBox { id: generationPlatform; Layout.preferredWidth: 130; Layout.preferredHeight: 34; model: publishPage.platformFilterOptions; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                    contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                                }
                                AppButton { text: publishPage.generationPending ? "生成中…" : "生成内容"; Layout.preferredWidth: 100; Layout.preferredHeight: 34; enabled: generationKeyword.text.trim() !== "" && !publishPage.generationPending; onClicked: { publishPage.generationPending = true; publishPage.generationNotice = "正在调用智能 API…"; backend.generateContent(generationKeyword.text, String(generationPlatform.currentValue || ""), "") }
                                    contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                    background: Rectangle { radius: 8; color: parent.enabled ? window.blue : window.panel2; border.color: window.line }
                                }
                            }
                        }
                    }
                    RowLayout { Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12
                        Rectangle { Layout.fillWidth: true; Layout.fillHeight: true; radius: 10; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 12; spacing: 8
                                Text { text: "生成结果"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium }
                                Text { text: backend.publishGeneratedTotal + " 条候选内容"; color: window.muted; font.pixelSize: 10 }
                                ListView { id: generatedContentList; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8; model: backend.publishGeneratedContents
                                            delegate: Rectangle { width: generatedContentList.width; height: (modelData.topics && modelData.topics.length > 0) || String(modelData.outline || "") !== "" ? 146 : 118; radius: 8; color: index % 2 === 0 ? "#172840" : "#132238"; border.color: window.line
                                                ColumnLayout { anchors.fill: parent; anchors.margins: 11; spacing: 5
                                                    RowLayout { Layout.fillWidth: true
                                                        Text { text: modelData.title || "未命名内容"; color: window.ink; font.pixelSize: 12; font.weight: Font.Medium; Layout.fillWidth: true; elide: Text.ElideRight }
                                                        AppButton { text: "加入发布"; Layout.preferredWidth: 76; Layout.preferredHeight: 28; onClicked: backend.importGeneratedContent(Number(modelData.id))
                                                            contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                            background: Rectangle { radius: 7; color: "transparent"; border.color: parent.hovered ? window.blue : window.line }
                                                        }
                                                        AppButton { text: "删除"; Layout.preferredWidth: 58; Layout.preferredHeight: 28; onClicked: backend.deleteGeneratedContent(Number(modelData.id))
                                                            contentItem: Text { text: parent.text; color: "#ff9b9b"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                            background: Rectangle { radius: 7; color: parent.hovered ? "#3a202b" : "transparent"; border.color: parent.hovered ? "#8b3f50" : window.line }
                                                        }
                                                    }
                                            Text { text: modelData.body || ""; color: "#c8d7ea"; font.pixelSize: 10; wrapMode: Text.WordWrap; maximumLineCount: 3; elide: Text.ElideRight; Layout.fillWidth: true; Layout.fillHeight: true }
                                            Text { visible: modelData.topics && modelData.topics.length > 0; text: "话题：" + ((modelData.topics || []).join("、")); color: window.muted; font.pixelSize: 9; elide: Text.ElideRight; Layout.fillWidth: true }
                                            Text { visible: String(modelData.outline || "") !== ""; text: "结构：" + String(modelData.outline || ""); color: window.muted; font.pixelSize: 9; elide: Text.ElideRight; Layout.fillWidth: true }
                                            Text { text: "平台：" + ((modelData.platforms || []).join("、")) + "  ·  来源：" + (modelData.source_type === "local_template" ? "本地模板" : "智能 API"); color: window.muted; font.pixelSize: 9 }
                                        }
                                    }
                                }
                                Text { visible: generatedContentList.count === 0; text: "输入选题后点击生成，结果会保存在这里"; color: window.muted; font.pixelSize: 11; Layout.alignment: Qt.AlignHCenter }
                            }
                        }
                        Rectangle { Layout.preferredWidth: 330; Layout.fillHeight: true; radius: 10; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 14; spacing: 10
                                Text { text: "生成流程"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium }
                                Text { text: "选题 → 结构化内容 → 平台版本 → 发布草稿"; color: window.blue; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                Text { text: "已接入选题生成系统的结构化流程：智能 API 生成完整正文、话题、大纲和评分；接口不可用时自动使用本地模板，不影响发布和采集。"; color: window.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                Item { Layout.fillHeight: true }
                                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 80; radius: 8; color: "#111d30"; border.color: window.line
                                    ColumnLayout { anchors.fill: parent; anchors.margins: 10
                                        Text { text: "当前策略"; color: window.muted; font.pixelSize: 10 }
                                        Text { text: backend.diagnosticLlmApi.enabled && backend.diagnosticLlmApi.configured ? "已启用智能 API：生成结果将保存为可直接发布的完整正文" : "当前使用本地模板：生成失败也不会影响采集与互动"; color: window.ink; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                    }
                                }
                            }
                        }
                    }
                }

                ColumnLayout {
                    visible: publishPage.publishTab === "messages"
                    anchors.fill: parent; anchors.margins: 22; spacing: 12
                    RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 48
                        ColumnLayout { Layout.fillWidth: true; spacing: 2
                            Text { text: "消息中心"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                            Text { text: "同步四个平台账号的全部消息：评论、回复、点赞、@、关注、私信、群通知和系统通知"; color: window.muted; font.pixelSize: 12; elide: Text.ElideRight }
                        }
                        Text { text: "未读 " + backend.publishMessageUnread; color: window.amber; font.pixelSize: 12 }
                        Text { visible: publishPage.messageActionNotice.length > 0; text: publishPage.messageActionNotice; color: publishPage.messageActionNotice.indexOf("失败") >= 0 ? window.amber : window.green; font.pixelSize: 10; elide: Text.ElideRight; Layout.preferredWidth: 260 }
                        AppButton { text: "同步消息"; Layout.preferredWidth: 88; Layout.preferredHeight: 32; onClicked: backend.syncPublishedMessages(publishPage.messagePlatform, publishPage.messageAccountId, publishPage.messageType, publishPage.messageUnreadOnly)
                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                            background: Rectangle { radius: 7; color: parent.hovered ? "#8bb8ff" : window.blue; border.color: window.blue }
                        }
                        AppButton { text: "刷新列表"; Layout.preferredWidth: 82; Layout.preferredHeight: 32; onClicked: backend.refreshPublishedMessages(1, publishPage.messagePlatform, publishPage.messageAccountId, publishPage.messageUnreadOnly, publishPage.messageType)
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                            background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 54; radius: 9; color: window.panel; border.color: window.line
                        RowLayout { anchors.fill: parent; anchors.margins: 10; spacing: 8
                            Text { text: "平台"; color: window.muted; font.pixelSize: 11 }
                            ComboBox { id: messagePlatformChooser; Layout.preferredWidth: 130; Layout.preferredHeight: 32; model: publishPage.platformFilterOptions; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                onActivated: { publishPage.messagePlatform = String(currentValue || ""); publishPage.resetMessageAccountForPlatform(); messageAccountChooser.currentIndex = 0; backend.refreshPublishedMessages(1, publishPage.messagePlatform, publishPage.messageAccountId, publishPage.messageUnreadOnly, publishPage.messageType) }
                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                            }
                            Text { text: "账号"; color: window.muted; font.pixelSize: 11; Layout.leftMargin: 8 }
                            ComboBox { id: messageAccountChooser; Layout.preferredWidth: 210; Layout.preferredHeight: 32; model: publishPage.messageAccountOptions(); textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                onActivated: { publishPage.messageAccountId = String(currentValue || "0"); backend.refreshPublishedMessages(1, publishPage.messagePlatform, publishPage.messageAccountId, publishPage.messageUnreadOnly, publishPage.messageType) }
                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                            }
                            Text { text: "类型"; color: window.muted; font.pixelSize: 11; Layout.leftMargin: 8 }
                            ComboBox { id: messageTypeChooser; Layout.preferredWidth: 130; Layout.preferredHeight: 32; model: publishPage.messageTypeFilterOptions; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                onActivated: { publishPage.messageType = String(currentValue || ""); backend.refreshPublishedMessages(1, publishPage.messagePlatform, publishPage.messageAccountId, publishPage.messageUnreadOnly, publishPage.messageType) }
                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                            }
                            AppCheckBox { text: "仅看未读"; checked: publishPage.messageUnreadOnly; onToggled: { publishPage.messageUnreadOnly = checked; backend.refreshPublishedMessages(1, publishPage.messagePlatform, publishPage.messageAccountId, publishPage.messageUnreadOnly, publishPage.messageType) }
                                Layout.preferredHeight: 28
                            }
                            Item { Layout.fillWidth: true }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.fillHeight: true; radius: 10; color: window.panel; border.color: window.line
                        ColumnLayout { anchors.fill: parent; anchors.margins: 12; spacing: 8
                            RowLayout { Layout.fillWidth: true
                                Text { text: "会话列表"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium; Layout.fillWidth: true }
                                Text { text: backend.publishMessageTotal + " 条消息 · " + (backend.publishMessageGroups || []).length + " 个会话"; color: window.muted; font.pixelSize: 10 }
                                Text { text: publishPage.messageSyncStatusText(); color: backend.publishMessageSyncStatus === "failed" ? window.amber : window.muted; font.pixelSize: 10; elide: Text.ElideRight }
                            }
                            RowLayout { Layout.fillWidth: true; Layout.fillHeight: true; spacing: 10
                                Rectangle { Layout.preferredWidth: 280; Layout.fillHeight: true; radius: 8; color: "#101d31"; border.color: window.line
                                    ColumnLayout { anchors.fill: parent; anchors.margins: 8; spacing: 6
                                        Text { text: "联系人"; color: window.muted; font.pixelSize: 11; Layout.leftMargin: 4 }
                                        ListView { id: messageGroupList; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 5; model: backend.publishMessageGroups
                                            onCountChanged: publishPage.ensureMessageGroupSelection()
                                            delegate: Rectangle { width: messageGroupList.width; height: 72; radius: 8
                                                property bool active: String(modelData.key || "") === publishPage.selectedMessageGroupKey
                                                color: active ? "#294b80" : (groupMouse.containsMouse ? "#203754" : "#15263d")
                                                border.color: active ? window.blue : (groupMouse.containsMouse ? "#43628e" : "transparent")
                                                RowLayout { anchors.fill: parent; anchors.margins: 8; spacing: 8
                                                    Loader { Layout.preferredWidth: 26; Layout.preferredHeight: 26; sourceComponent: platformIconComponent; onLoaded: item.platform = modelData.platform }
                                                    ColumnLayout { Layout.fillWidth: true; spacing: 2
                                                        RowLayout { Layout.fillWidth: true
                                                            Text { text: modelData.nickname || "匿名用户"; color: window.ink; font.pixelSize: 11; font.weight: Font.Medium; Layout.fillWidth: true; elide: Text.ElideRight }
                                                            Rectangle { visible: Number(modelData.unread_count || 0) > 0; Layout.preferredWidth: 20; Layout.preferredHeight: 18; radius: 9; color: window.amber
                                                                Text { anchors.centerIn: parent; text: modelData.unread_count; color: "#071224"; font.pixelSize: 9; font.weight: Font.Bold }
                                                            }
                                                        }
                                                        Text { text: "ID：" + (modelData.display_id || "未获取ID"); color: window.muted; font.pixelSize: 9; elide: Text.ElideMiddle; wrapMode: Text.NoWrap; clip: true; Layout.fillWidth: true; Layout.minimumWidth: 0 }
                                                        Text { text: (modelData.platform_label || "平台") + " · " + (modelData.message_count || 0) + " 条 · " + (modelData.latest_preview || "暂无消息"); color: window.blue; font.pixelSize: 9; elide: Text.ElideRight; Layout.fillWidth: true }
                                                    }
                                                }
                                                MouseArea { id: groupMouse; anchors.fill: parent; hoverEnabled: true; onClicked: publishPage.selectMessageGroup(modelData.key) }
                                            }
                                        }
                                        Text { visible: messageGroupList.count === 0; text: "暂无会话"; color: window.muted; font.pixelSize: 10; Layout.alignment: Qt.AlignHCenter }
                                    }
                                }
                                Rectangle { Layout.fillWidth: true; Layout.fillHeight: true; radius: 8; color: "#101d31"; border.color: window.line
                                    ColumnLayout { anchors.fill: parent; anchors.margins: 12; spacing: 8
                                        RowLayout { Layout.fillWidth: true
                                            ColumnLayout { Layout.fillWidth: true; spacing: 2
                                                Text { text: publishPage.selectedMessageGroup().nickname || "请选择左侧会话"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                                Text { text: publishPage.selectedMessageGroup().user_id ? "ID：" + publishPage.selectedMessageGroup().user_id : "ID：未获取ID"; color: window.muted; font.pixelSize: 10; elide: Text.ElideMiddle; wrapMode: Text.NoWrap; clip: true; Layout.fillWidth: true; Layout.minimumWidth: 0 }
                                            }
                                            Text { text: (publishPage.selectedMessageGroup().platform_label || "") + (publishPage.selectedMessageGroup().account_name ? " · " + publishPage.selectedMessageGroup().account_name : ""); color: window.blue; font.pixelSize: 10; elide: Text.ElideRight }
                                        }
                                        Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
                                        ListView { id: messageDetailList; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 7; model: publishPage.selectedMessageGroupRows()
                                            delegate: Rectangle { width: messageDetailList.width; height: modelData.can_reply ? 166 : 132; radius: 8; color: modelData.is_read ? "#132238" : "#1c304d"; border.color: modelData.is_read ? window.line : window.blue
                                                ColumnLayout { anchors.fill: parent; anchors.margins: 10; spacing: 5
                                                    RowLayout { Layout.fillWidth: true
                                                        Text { text: (modelData.message_type_label || "其他") + (modelData.action ? " · " + modelData.action : ""); color: window.blue; font.pixelSize: 10; Layout.fillWidth: true; elide: Text.ElideRight }
                                                        Text { text: modelData.event_time || modelData.created_at || ""; color: window.muted; font.pixelSize: 9 }
                                                        Rectangle { radius: 5; color: modelData.can_reply ? "#173f4c" : "#27354a"; Layout.preferredWidth: 48; Layout.preferredHeight: 20
                                                            Text { anchors.centerIn: parent; text: modelData.can_reply ? "可回复" : "仅通知"; color: modelData.can_reply ? window.green : window.muted; font.pixelSize: 9 }
                                                        }
                                                    }
                                                    Text { text: modelData.content || "暂无文字内容（可能是点赞、关注或系统通知）"; color: "#c8d7ea"; font.pixelSize: 11; wrapMode: Text.WordWrap; maximumLineCount: 3; elide: Text.ElideRight; Layout.fillWidth: true; Layout.fillHeight: true }
                                                    Text { visible: Boolean(modelData.quote_content || modelData.source_title); text: (modelData.quote_content ? "原文：" + modelData.quote_content : "来源：" + modelData.source_title); color: window.muted; font.pixelSize: 9; maximumLineCount: 1; elide: Text.ElideRight; Layout.fillWidth: true }
                                                    RowLayout { Layout.fillWidth: true
                                                        Item { Layout.fillWidth: true }
                                                        AppButton { visible: modelData.can_reply; enabled: publishPage.messageActionPendingId === 0; text: publishPage.messageActionPendingId === Number(modelData.id) ? "打开中…" : "打开浏览器回复"; Layout.preferredWidth: 112; Layout.preferredHeight: 26; onClicked: { publishPage.messageActionPendingId = Number(modelData.id || 0); publishPage.messageActionNotice = "正在打开对应浏览器…"; backend.openPublishedMessageReply(Number(modelData.id)) }
                                                            contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                            background: Rectangle { radius: 7; color: parent.hovered ? "#203b61" : "transparent"; border.color: parent.hovered ? window.blue : window.line }
                                                        }
                                                        AppButton { text: modelData.is_read ? "已读" : "标记已读"; Layout.preferredWidth: 76; Layout.preferredHeight: 26; onClicked: backend.markPublishedMessage(Number(modelData.id), true)
                                                            contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                                            background: Rectangle { radius: 7; color: "transparent"; border.color: parent.hovered ? window.blue : window.line }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        Text { visible: messageDetailList.count === 0; text: messageGroupList.count === 0 ? "暂无已同步消息，点击右上角“同步消息”读取四个平台的全部消息" : "该会话暂无消息"; color: window.muted; font.pixelSize: 11; Layout.alignment: Qt.AlignHCenter }
                                    }
                                }
                            }
                        }
                    }
                }

                ColumnLayout {
                    visible: publishPage.publishTab === "settings"
                    anchors.fill: parent; anchors.margins: 22; spacing: 12
                    RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 48
                        ColumnLayout { Layout.fillWidth: true; spacing: 2
                            Text { text: "发布设置"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                            Text { text: "连接、API 和操作日志沿用采集工作台的统一设置"; color: window.muted; font.pixelSize: 12 }
                        }
                        AppButton { text: "刷新状态"; Layout.preferredWidth: 88; Layout.preferredHeight: 32; onClicked: backend.refreshDiagnostics()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                            background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                        }
                    }
                    RowLayout { Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12
                        Rectangle { Layout.fillWidth: true; Layout.fillHeight: true; radius: 10; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 12
                                Text { text: "服务状态"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                Repeater { model: [{label: "BitBrowser", value: backend.diagnosticBitBrowser.configured ? "已配置" : "未配置", ok: backend.diagnosticBitBrowser.configured}, {label: "智能 API", value: backend.diagnosticLlmApi.configured ? "已配置" : "未配置", ok: backend.diagnosticLlmApi.configured}, {label: "后台日志", value: backend.connectionText, ok: backend.connectionText === "后台服务已连接"}]
                                    delegate: Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 54; radius: 8; color: "#111d30"; border.color: window.line
                                        RowLayout { anchors.fill: parent; anchors.margins: 12
                                            Text { text: modelData.label; color: window.muted; Layout.fillWidth: true; font.pixelSize: 11 }
                                            Text { text: modelData.value; color: modelData.ok ? window.green : window.amber; font.pixelSize: 11 }
                                        }
                                    }
                                }
                                Item { Layout.fillHeight: true }
                                Text { text: "发布工作区默认只创建草稿和队列，不会绕过现有的真实发送确认。"; color: window.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                            }
                        }
                        Rectangle { Layout.preferredWidth: 360; Layout.fillHeight: true; radius: 10; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 9
                                Text { text: "智能回复与评论分析 API"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                Text { text: "配置后用于批量意向分析和后续智能内容生成"; color: window.muted; font.pixelSize: 10; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 30; spacing: 7
                                    Text { text: "服务商"; color: window.muted; Layout.preferredWidth: 52; font.pixelSize: 10 }
                                    TextField { id: publishApiProviderField; Layout.fillWidth: true; text: backend.diagnosticLlmApi.provider || ""; color: window.ink; palette.placeholderText: window.muted; placeholderText: "例如：DeepSeek"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                                }
                                RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 30; spacing: 7
                                    Text { text: "API 地址"; color: window.muted; Layout.preferredWidth: 52; font.pixelSize: 10 }
                                    TextField { id: publishApiUrlField; Layout.fillWidth: true; text: backend.diagnosticLlmApi.base_url || ""; color: window.ink; palette.placeholderText: window.muted; placeholderText: "https://.../v1"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                                }
                                RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 30; spacing: 7
                                    Text { text: "模型"; color: window.muted; Layout.preferredWidth: 52; font.pixelSize: 10 }
                                    TextField { id: publishApiModelField; Layout.fillWidth: true; text: backend.diagnosticLlmApi.model || ""; color: window.ink; palette.placeholderText: window.muted; placeholderText: "模型名称"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                                }
                                RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 30; spacing: 7
                                    Text { text: "API Key"; color: window.muted; Layout.preferredWidth: 52; font.pixelSize: 10 }
                                    TextField { id: publishApiKeyField; Layout.fillWidth: true; echoMode: TextInput.Password; color: window.ink; palette.placeholderText: window.muted; placeholderText: "留空表示保留已保存密钥"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                                }
                                RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 30; spacing: 8
                                    AppCheckBox { id: publishApiEnabledCheck; text: "启用智能 API"; checked: Boolean(backend.diagnosticLlmApi.enabled); Layout.fillWidth: true }
                                    AppButton { text: "保存 API"; Layout.preferredWidth: 78; Layout.preferredHeight: 28; onClicked: backend.saveSettings(String(backend.diagnosticBitBrowser.base_url || "http://127.0.0.1:54345"), Number(backend.diagnosticBitBrowser.timeout || 20), publishApiProviderField.text, publishApiUrlField.text, publishApiKeyField.text, publishApiModelField.text, publishApiEnabledCheck.checked)
                                        contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                        background: Rectangle { radius: 7; color: window.blue; border.color: window.line }
                                    }
                                }
                                Text { text: backend.diagnosticLlmApi.configured ? "当前 API 已配置，可进行连通性测试" : "请填写 API 地址和密钥后保存"; color: backend.diagnosticLlmApi.configured ? window.green : window.amber; font.pixelSize: 10; elide: Text.ElideRight; Layout.fillWidth: true }
                                Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
                                Text { text: "链路测试"; color: window.ink; font.pixelSize: 13; font.weight: Font.Medium }
                                AppButton { text: "检测 BitBrowser"; Layout.fillWidth: true; Layout.preferredHeight: 32; onClicked: backend.inspectBitBrowser()
                                    contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                                }
                                AppButton { text: "测试智能 API"; Layout.fillWidth: true; Layout.preferredHeight: 32; onClicked: backend.testLlmApi()
                                    contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                                }
                                AppButton { text: "运行平台链路测试"; Layout.fillWidth: true; Layout.preferredHeight: 32; onClicked: backend.runPlatformHealth()
                                    contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                                }
                                AppButton { text: "导出操作日志"; Layout.fillWidth: true; Layout.preferredHeight: 32; onClicked: backend.exportOperationLog()
                                    contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                    background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                                }
                                Item { Layout.fillHeight: true }
                            }
                        }
                    }
                }

                ColumnLayout {
                    visible: publishPage.publishTab === "publish"
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 52
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text { text: "发布中心"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                            Text { text: "统一管理四个平台的内容版本、素材、审核和发布队列"; color: window.muted; font.pixelSize: 12 }
                        }
                        Item { Layout.fillWidth: true }
                        AppButton {
                            text: publishPage.realPublishEnabled ? "真实发布：已开启" : "开启真实发布"
                            Layout.preferredWidth: 132; Layout.preferredHeight: 34
                            onClicked: {
                                if (publishPage.realPublishEnabled) publishPage.realPublishEnabled = false
                                else realPublishEnableDialog.open()
                            }
                            contentItem: Text { text: parent.text; color: publishPage.realPublishEnabled ? "#ff9696" : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 17; color: publishPage.realPublishEnabled ? "#3a1f2b" : "#101b2d"; border.color: publishPage.realPublishEnabled ? "#8b4d5a" : (parent.hovered ? window.blue : window.line) }
                        }
                        AppButton {
                            text: "刷新"
                            Layout.preferredWidth: 88; Layout.preferredHeight: 34
                            onClicked: publishPage.requestRefresh()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                        }
                        AppButton {
                            text: "从内容生成导入"
                            Layout.preferredWidth: 126; Layout.preferredHeight: 34
                            onClicked: publishPage.selectTab("generation")
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                        }
                        AppButton {
                            text: "＋ 新建内容"
                            Layout.preferredWidth: 138; Layout.preferredHeight: 34
                            onClicked: publishDraftDialog.open()
                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: parent.hovered ? "#8bbaff" : window.blue }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 48
                        radius: 9; color: window.panel; border.color: window.line
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 10; anchors.rightMargin: 10; spacing: 8
                            ComboBox {
                                id: publishStatusChooser
                                Layout.preferredWidth: 148; Layout.preferredHeight: 32
                                model: [{value: "all", label: "全部状态"}].concat(backend.publishStatusOptions)
                                textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                onActivated: { publishPage.activeStatus = String(currentValue || "all"); publishPage.requestRefresh() }
                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                            }
                            ComboBox {
                                id: publishPlatformChooser
                                Layout.preferredWidth: 148; Layout.preferredHeight: 32
                                model: publishPage.platformFilterOptions
                                textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                onActivated: { publishPage.activePlatform = String(currentValue || ""); publishPage.requestRefresh() }
                                contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 10 }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                            }
                            TextField {
                                id: publishSearchField
                                Layout.preferredWidth: 300; Layout.preferredHeight: 32
                                leftPadding: 32; placeholderText: "搜索标题、内容或来源"
                                color: window.ink; placeholderTextColor: "#7187a3"
                                onAccepted: { publishPage.searchText = text; publishPage.requestRefresh() }
                                background: Rectangle { radius: 7; color: "#111d30"; border.color: parent.activeFocus ? window.blue : window.line }
                                Text { anchors.left: parent.left; anchors.leftMargin: 11; anchors.verticalCenter: parent.verticalCenter; text: "⌕"; color: window.muted; font.pixelSize: 17 }
                            }
                            AppButton {
                                text: "搜索"; Layout.preferredWidth: 64; Layout.preferredHeight: 32
                                onClicked: { publishPage.searchText = publishSearchField.text; publishPage.requestRefresh() }
                                contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                background: Rectangle { radius: 7; color: "transparent"; border.color: parent.hovered ? window.blue : window.line }
                            }
                            Item { Layout.fillWidth: true }
                            Text { text: backend.publishTotal + " 条内容"; color: window.muted; font.pixelSize: 11 }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 10
                        Rectangle {
                            Layout.preferredWidth: 300
                            Layout.minimumWidth: 280
                            Layout.fillHeight: true
                            radius: 10; color: window.panel; border.color: window.line
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 12; spacing: 8
                                RowLayout { Layout.fillWidth: true
                                    Text { text: "内容列表"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                    Item { Layout.fillWidth: true }
                                    Text { text: "草稿箱"; color: window.muted; font.pixelSize: 10 }
                                }
                                ListView {
                                    id: publishDraftList
                                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8
                                    model: backend.publishDraftRows
                                    delegate: Rectangle {
                                        width: publishDraftList.width; height: 94; radius: 9
                                        property var rowPlatformValues: publishPage.rowPlatforms(modelData)
                                        property var rowPlatformLabels: publishPage.rowPlatformLabels(modelData)
                                        color: Number(modelData.id || 0) === publishPage.selectedDraftId ? "#1d3d68" : (index % 2 === 0 ? "#111d30" : "#17273d")
                                        border.color: Number(modelData.id || 0) === publishPage.selectedDraftId ? window.blue : window.line
                                        border.width: Number(modelData.id || 0) === publishPage.selectedDraftId ? 1 : 1
                                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: publishPage.loadDraft(modelData) }
                                        ColumnLayout { anchors.fill: parent; anchors.margins: 10; spacing: 4
                                            RowLayout { Layout.fillWidth: true; spacing: 8
                                                Row {
                                                    Layout.preferredWidth: 92
                                                    Layout.preferredHeight: 26
                                                    spacing: 3
                                                    Repeater {
                                                        model: rowPlatformValues
                                                        delegate: Loader {
                                                            width: 20; height: 20
                                                            sourceComponent: userPlatformIconComponent
                                                            onLoaded: item.platform = modelData
                                                        }
                                                    }
                                                }
                                                ColumnLayout { Layout.fillWidth: true; spacing: 2
                                                    Text { text: modelData.title || "未命名内容"; color: window.ink; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 12; font.weight: Font.Medium }
                                                    Text { text: "来源：线索中心 · 内容发布"; color: window.muted; font.pixelSize: 9; elide: Text.ElideRight }
                                                }
                                                Text { text: "⋮"; color: window.muted; font.pixelSize: 18 }
                                            }
                                            RowLayout { Layout.fillWidth: true; spacing: 7
                                                Text { text: rowPlatformLabels.join(" · ") || "未选择平台"; color: window.muted; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 9 }
                                                Rectangle { Layout.preferredWidth: 52; Layout.preferredHeight: 21; radius: 6; color: "#332d1d"; border.color: publishPage.statusColor(modelData.status)
                                                    Text { anchors.centerIn: parent; text: modelData.status_label || "草稿"; color: publishPage.statusColor(modelData.status); font.pixelSize: 9 }
                                                }
                                            }
                                            Text { text: "素材 " + (modelData.asset_count || 0) + " 个 · 更新于 " + publishPage.displayTime(modelData.updated_at); color: "#7187a3"; font.pixelSize: 9 }
                                        }
                                    }
                                }
                                Text { visible: backend.publishDraftRows.length === 0; text: "暂无内容，点击右上角新建内容"; color: window.muted; Layout.alignment: Qt.AlignHCenter; font.pixelSize: 11 }
                            }
                        }
                        Rectangle {
                            Layout.fillWidth: true; Layout.minimumWidth: 480; Layout.fillHeight: true
                            radius: 10; color: window.panel; border.color: window.line
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 14; spacing: 8
                                RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 28
                                    Text { text: publishPage.selectedDraftId > 0 ? "平台版本编辑" : "选择一条内容"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                    Item { Layout.fillWidth: true }
                                    Text { visible: publishPage.selectedDraftId > 0; text: "版本 v" + ((publishPage.selectedRow() || {}).version || 1); color: window.muted; font.pixelSize: 10 }
                                }
                                RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 38; spacing: 4
                                    Repeater {
                                        model: publishPage.platformTabs
                                        delegate: AppButton {
                                            Layout.fillWidth: true; Layout.fillHeight: true
                                            onClicked: {
                                                var nextPlatform = String(modelData.value || "douyin")
                                                if (publishPage.selectedPlatform !== nextPlatform) {
                                                    publishPage.selectedPlatform = nextPlatform
                                                    publishPage.resetPublishAccountForPlatform()
                                                }
                                                publishPage.loadPlatformVariant()
                                            }
                                            contentItem: RowLayout { spacing: 5
                                                Loader { Layout.preferredWidth: 20; Layout.preferredHeight: 20; sourceComponent: userPlatformIconComponent; onLoaded: item.platform = modelData.value }
                                                Text { text: modelData.label; color: publishPage.selectedPlatform === modelData.value ? window.ink : window.muted; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                            }
                                            background: Rectangle { color: "transparent"; border.color: "transparent"
                                                Rectangle { anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter; width: parent.width - 10; height: 2; color: publishPage.selectedPlatform === modelData.value ? window.blue : "transparent" }
                                            }
                                        }
                                    }
                                }
                                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: window.line }
                                Text { visible: publishPage.selectedDraftId === 0; text: "左侧选择一条内容后，在这里分别编辑四个平台版本"; color: window.muted; Layout.alignment: Qt.AlignHCenter; font.pixelSize: 12 }
                                ColumnLayout {
                                    visible: publishPage.selectedDraftId > 0
                                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: 9
                                    RowLayout { Layout.fillWidth: true
                                        Text { text: "标题"; color: window.muted; Layout.preferredWidth: 56; font.pixelSize: 11 }
                                        TextField { id: publishEditorTitle; Layout.fillWidth: true; Layout.preferredHeight: 34; text: publishPage.editorTitle; onTextChanged: publishPage.editorTitle = text; color: window.ink; leftPadding: 10; background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line } }
                                        Text { text: String(publishPage.editorTitle || "").length + "/60"; color: window.muted; font.pixelSize: 9 }
                                    }
                                    RowLayout { Layout.fillWidth: true
                                        Text { text: "正文内容"; color: window.muted; font.pixelSize: 11; Layout.fillWidth: true }
                                        Text { text: String(publishPage.editorBody || "").length + "/1000"; color: window.muted; font.pixelSize: 9 }
                                    }
                                    TextArea { id: publishEditorBody; Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 130; text: publishPage.editorBody; onTextChanged: publishPage.editorBody = text; color: window.ink; wrapMode: TextArea.Wrap; selectByMouse: true; placeholderText: "请输入完整发布内容"; placeholderTextColor: "#7187a3"; leftPadding: 10; topPadding: 10; background: Rectangle { radius: 7; color: "#111d30"; border.color: parent.activeFocus ? window.blue : window.line } }
                                    RowLayout { Layout.fillWidth: true
                                        Text { text: "话题标签"; color: window.muted; Layout.preferredWidth: 56; font.pixelSize: 11 }
                                        TextField { id: publishEditorTopics; Layout.fillWidth: true; Layout.preferredHeight: 34; text: publishPage.editorTopics; onTextChanged: publishPage.editorTopics = text; color: window.ink; placeholderText: "多个话题用逗号分隔"; placeholderTextColor: "#7187a3"; leftPadding: 10; background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line } }
                                    }
                                    RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 92; spacing: 8
                                        Text { text: "素材"; color: window.muted; Layout.preferredWidth: 56; Layout.alignment: Qt.AlignTop; topPadding: 7; font.pixelSize: 11 }
                                        Repeater {
                                            model: publishPage.assetRows().slice(0, 3)
                                            delegate: Rectangle { Layout.preferredWidth: 82; Layout.preferredHeight: 82; radius: 8; color: index % 2 === 0 ? "#203a48" : "#27344f"; border.color: window.line
                                                Text { anchors.centerIn: parent; width: parent.width - 10; text: publishPage.assetName(modelData, index); color: "#d7e8f5"; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideMiddle; font.pixelSize: 9 }
                                                AppButton {
                                                    anchors.right: parent.right; anchors.top: parent.top; anchors.rightMargin: 3; anchors.topMargin: 2
                                                    width: 22; height: 22; text: "×"
                                                    onClicked: if (Number(modelData.id || 0) > 0) backend.deletePublishAsset(publishPage.selectedDraftId, Number(modelData.id))
                                                    contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14 }
                                                    background: Rectangle { radius: 5; color: parent.hovered ? "#3a2d3c" : "transparent" }
                                                }
                                            }
                                        }
                                        AppButton {
                                            text: "＋ 添加素材"; Layout.preferredWidth: 92; Layout.preferredHeight: 82
                                            contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                            background: Rectangle {
                                                radius: 8; color: "transparent"; border.color: parent.hovered ? window.blue : window.line; border.width: 1
                                                Text { anchors.horizontalCenter: parent.horizontalCenter; anchors.top: parent.top; anchors.topMargin: 13; text: "+"; color: window.blue; font.pixelSize: 21 }
                                            }
                                            onClicked: publishAssetDialog.open()
                                        }
                                        Item { Layout.fillWidth: true }
                                    }
                                    RowLayout { Layout.fillWidth: true
                                        Item { Layout.fillWidth: true }
                                        AppButton {
                                            text: "保存平台版本"; Layout.preferredWidth: 116; Layout.preferredHeight: 34
                                            onClicked: backend.updatePublishVariant(publishPage.selectedDraftId, publishPage.selectedPlatform, publishPage.editorTitle, publishPage.editorBody, publishPage.editorTopics, "text")
                                            contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                            background: Rectangle { radius: 7; color: "transparent"; border.color: parent.hovered ? window.blue : window.line }
                                        }
                                        AppButton {
                                            text: "提交审核"; Layout.preferredWidth: 92; Layout.preferredHeight: 34; visible: (publishPage.selectedRow() || {}).status === "draft"
                                            onClicked: backend.publishDraftAction(publishPage.selectedDraftId, "submit_review")
                                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                            background: Rectangle { radius: 7; color: parent.hovered ? "#8bbaff" : window.blue }
                                        }
                                        AppButton {
                                            text: "批准"; Layout.preferredWidth: 72; Layout.preferredHeight: 34; visible: (publishPage.selectedRow() || {}).status === "review"
                                            onClicked: backend.publishDraftAction(publishPage.selectedDraftId, "approve")
                                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                            background: Rectangle { radius: 7; color: parent.hovered ? "#64e3b7" : window.green }
                                        }
                                        AppButton {
                                            text: "进入待发布"; Layout.preferredWidth: 102; Layout.preferredHeight: 34; visible: (publishPage.selectedRow() || {}).status === "approved"
                                            onClicked: backend.publishDraftAction(publishPage.selectedDraftId, "queue")
                                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                            background: Rectangle { radius: 7; color: parent.hovered ? "#8bbaff" : window.blue }
                                        }
                                        AppButton {
                                            text: "删除"; Layout.preferredWidth: 72; Layout.preferredHeight: 34; visible: publishPage.selectedDraftId > 0 && (publishPage.selectedRow() || {}).status !== "published"
                                            onClicked: backend.deletePublishDraft(publishPage.selectedDraftId)
                                            contentItem: Text { text: parent.text; color: "#ff9b9b"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                            background: Rectangle { radius: 7; color: "transparent"; border.color: parent.hovered ? "#ee7182" : window.line }
                                        }
                                    }
                                }
                            }
                        }
                        Rectangle {
                            Layout.preferredWidth: 302; Layout.minimumWidth: 280; Layout.fillHeight: true
                            radius: 10; color: window.panel; border.color: window.line
                            Flickable {
                                id: publishSideScroll
                                anchors.fill: parent
                                anchors.margins: 14
                                clip: true
                                contentWidth: width
                                contentHeight: publishSideColumn.implicitHeight
                                boundsBehavior: Flickable.StopAtBounds
                                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                                ColumnLayout {
                                    id: publishSideColumn
                                    width: publishSideScroll.width
                                    spacing: 8
                                RowLayout { Layout.fillWidth: true; Layout.preferredHeight: 26
                                    Text { text: "平台预览"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                    Item { Layout.fillWidth: true }
                                    Text { text: publishPage.platformLabel(publishPage.selectedPlatform); color: window.blue; font.pixelSize: 10 }
                                }
                                Rectangle {
                                    Layout.fillWidth: true; Layout.preferredHeight: 276; radius: 10; color: "#101b2d"; border.color: window.line
                                    ColumnLayout { anchors.fill: parent; anchors.margins: 10; spacing: 7
                                        RowLayout { Layout.fillWidth: true
                                            Loader {
                                                property string platformValue: publishPage.selectedPlatform
                                                Layout.preferredWidth: 26; Layout.preferredHeight: 26
                                                sourceComponent: userPlatformIconComponent
                                                onLoaded: if (item) item.platform = platformValue
                                                onPlatformValueChanged: if (item) item.platform = platformValue
                                            }
                                            ColumnLayout { Layout.fillWidth: true; spacing: 1
                                                Text { text: publishPage.selectedAccountName(); color: window.ink; font.pixelSize: 10; font.weight: Font.Medium }
                                                Text { text: "刚刚"; color: window.muted; font.pixelSize: 9 }
                                            }
                                            Text { text: "•••"; color: window.muted; font.pixelSize: 12 }
                                        }
                                        Text { text: publishPage.editorTitle || "标题预览"; color: window.ink; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 13; font.weight: Font.Medium }
                                        Text { text: publishPage.editorBody || "预览内容将在选择内容后显示"; color: "#c8d7ea"; wrapMode: Text.WordWrap; Layout.fillWidth: true; Layout.maximumHeight: 44; elide: Text.ElideRight; font.pixelSize: 10; verticalAlignment: Text.AlignTop }
                                        Rectangle {
                                            id: previewMediaFrame
                                            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 90
                                            radius: 8; color: "#203a48"; border.color: "#2e5362"
                                            property string mediaUrl: publishPage.previewAssetUrl()
                                            property bool mediaIsVideo: publishPage.previewAssetIsVideo()
                                            property bool hasMedia: mediaUrl !== ""
                                            clip: true
                                            Image {
                                                id: previewImage
                                                anchors.fill: parent; anchors.margins: 2
                                                visible: previewMediaFrame.hasMedia && !previewMediaFrame.mediaIsVideo
                                                source: previewMediaFrame.mediaUrl
                                                fillMode: Image.PreserveAspectFit
                                                smooth: true; mipmap: true; asynchronous: true
                                            }
                                            MediaPlayer {
                                                id: previewVideoPlayer
                                                source: previewMediaFrame.mediaIsVideo ? previewMediaFrame.mediaUrl : ""
                                                videoOutput: previewVideoOutput
                                                autoPlay: previewMediaFrame.mediaIsVideo
                                                loops: MediaPlayer.Infinite
                                            }
                                            VideoOutput {
                                                id: previewVideoOutput
                                                anchors.fill: parent; anchors.margins: 2
                                                visible: previewMediaFrame.hasMedia && previewMediaFrame.mediaIsVideo
                                                fillMode: VideoOutput.PreserveAspectFit
                                            }
                                            Rectangle {
                                                anchors.fill: parent; color: "#101b2d"; opacity: 0.45
                                                visible: previewMediaFrame.hasMedia && previewMediaFrame.mediaIsVideo
                                            }
                                            ColumnLayout {
                                                anchors.centerIn: parent; spacing: 4
                                                visible: !previewMediaFrame.hasMedia
                                                Text { text: "▣"; color: "#9fcbd0"; font.pixelSize: 28; Layout.alignment: Qt.AlignHCenter }
                                                Text { text: "暂无素材"; color: "#bad4dc"; font.pixelSize: 10 }
                                            }
                                            Text {
                                                anchors.centerIn: parent; visible: previewImage.visible && previewImage.status === Image.Error
                                                text: "图片无法预览"; color: "#bad4dc"; font.pixelSize: 10
                                            }
                                            Text {
                                                anchors.centerIn: parent; visible: previewVideoOutput.visible && previewVideoPlayer.error !== MediaPlayer.NoError
                                                text: "视频无法预览"; color: "#bad4dc"; font.pixelSize: 10
                                            }
                                        }
                                        Text { text: "♡  点赞       ○  评论       ↗  分享       ☆  收藏"; color: window.muted; font.pixelSize: 9 }
                                    }
                                }
                                Text { text: "发布设置"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium; Layout.topMargin: 2 }
                                RowLayout { Layout.fillWidth: true
                                    Text { text: "选择账号"; color: window.muted; Layout.preferredWidth: 58; font.pixelSize: 10 }
                                    ComboBox {
                                        id: publishAccountChooser; Layout.fillWidth: true; Layout.preferredHeight: 32
                                        model: publishPage.accountOptions(); textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                        onActivated: {
                                            publishPage.selectedAccountId = String(currentValue || "0")
                                            publishPage.selectedAccountIsValid()
                                        }
                                        onModelChanged: publishPage.ensurePublishAccountSelection()
                                        contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight }
                                        background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                                    }
                                }
                                RowLayout { Layout.fillWidth: true
                                    Text { text: "发布时间"; color: window.muted; Layout.preferredWidth: 58; font.pixelSize: 10 }
                                    ComboBox {
                                        id: publishTimeModeChooser
                                        Layout.fillWidth: true; Layout.preferredHeight: 32
                                        model: [{label: "立即发布", value: "now"}, {label: "定时发布", value: "scheduled"}]; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                                        onActivated: {
                                            publishPage.publishTimeMode = String(currentValue || "now")
                                            if (publishPage.publishTimeMode === "scheduled")
                                                publishPage.ensureSchedulePickerSelection()
                                        }
                                        contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 }
                                        background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                                    }
                                }
                                RowLayout { Layout.fillWidth: true; visible: publishPage.publishTimeMode === "scheduled"
                                    Text { text: "定时于"; color: window.muted; Layout.preferredWidth: 58; font.pixelSize: 10 }
                                    AppButton {
                                        Layout.fillWidth: true; Layout.preferredHeight: 32
                                        text: publishPage.scheduleDisplayText()
                                        onClicked: publishPage.openSchedulePicker()
                                        contentItem: Text {
                                            text: parent.text
                                            color: window.ink
                                            verticalAlignment: Text.AlignVCenter
                                            leftPadding: 10
                                            elide: Text.ElideRight
                                            font.pixelSize: 10
                                        }
                                        background: Rectangle {
                                            radius: 7
                                            color: parent.hovered ? "#1e3555" : window.panel2
                                            border.color: parent.hovered ? window.blue : window.line
                                        }
                                    }
                                }
                                Text { visible: publishPage.publishTimeMode === "scheduled"; text: "北京时间 " + String(backend.beijingNowText || "").slice(0, 16) + " · 只能选择未来时间"; color: publishPage.scheduleSelectionIsFuture() ? window.muted : window.amber; font.pixelSize: 9; Layout.fillWidth: true }
                                AppCheckBox { text: "仅保存草稿"; checked: publishPage.saveDraftOnly; onToggled: publishPage.saveDraftOnly = checked; Layout.preferredHeight: 24 }
                                AppButton {
                                    text: publishPage.realPublishEnabled ? "保存定时发布" : "需开启真实发布"
                                    visible: publishPage.publishTimeMode === "scheduled"
                                    enabled: publishPage.selectedDraftId > 0 && publishPage.selectedAccountIsValid() && publishPage.scheduleSelectionIsFuture()
                                    Layout.fillWidth: true; Layout.preferredHeight: 34
                                    onClicked: {
                                        if (!publishPage.realPublishEnabled) {
                                            publishPage.scheduleNotice = "请先开启真实发布，再保存定时发布；否则到点不会点击平台最终发布按钮"
                                            return
                                        }
                                        publishPage.scheduleNotice = "正在保存定时发布，请稍候…"
                                        publishPage.scheduledAt = publishPage.buildScheduledAt()
                                        backend.schedulePublish(
                                            publishPage.selectedDraftId, publishPage.selectedPlatform,
                                            Number(publishPage.selectedAccountId), publishPage.scheduledAt,
                                            publishPage.editorTitle, publishPage.editorBody,
                                            publishPage.editorTopics, publishPage.realPublishEnabled)
                                    }
                                    contentItem: Text { text: parent.text; color: parent.enabled ? (publishPage.realPublishEnabled ? window.blue : window.amber) : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                    background: Rectangle { radius: 7; color: parent.enabled && parent.hovered ? "#1e3555" : "transparent"; border.color: parent.enabled && parent.hovered ? window.blue : window.line }
                                }
                                Text {
                                    visible: publishPage.publishTimeMode === "scheduled" && publishPage.scheduleNotice !== ""
                                    text: publishPage.scheduleNotice
                                    color: publishPage.scheduleNotice.indexOf("失败") >= 0 || publishPage.scheduleNotice.indexOf("不会") >= 0 ? window.amber : window.green
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                    Layout.minimumHeight: 22
                                    Layout.preferredHeight: Math.max(22, implicitHeight)
                                    lineHeight: 13
                                    lineHeightMode: Text.FixedHeight
                                    font.pixelSize: 9
                                }
                                AppButton {
                                    text: publishPage.realPublishEnabled ? "开始真实发布" : "填入浏览器预览"; Layout.fillWidth: true; Layout.preferredHeight: 34
                                    enabled: publishPage.realPublishEnabled ? publishPage.realPublishAllowed() : (publishPage.selectedDraftId > 0 && publishPage.selectedAccountIsValid())
                                    onClicked: {
                                        if (publishPage.realPublishEnabled) realPublishConfirmDialog.open()
                                         else backend.previewPublishDraft(
                                             publishPage.selectedDraftId, publishPage.selectedPlatform,
                                             Number(publishPage.selectedAccountId), true,
                                             publishPage.editorTitle, publishPage.editorBody,
                                             publishPage.editorTopics)
                                    }
                                    contentItem: Text { text: parent.text; color: parent.enabled ? (publishPage.realPublishEnabled ? "#071224" : window.blue) : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                    background: Rectangle { radius: 7; color: parent.enabled && publishPage.realPublishEnabled ? (parent.hovered ? "#8bbaff" : window.blue) : "transparent"; border.color: parent.enabled && parent.hovered ? window.blue : window.line }
                                }
                                Text {
                                        text: publishPage.realPublishEnabled
                                          ? (publishPage.realPublishAllowed()
                                             ? "已开启真实发布：点击上方按钮后会进入二次确认"
                                             : publishPage.realPublishDisabledReason())
                                          : "模拟模式：只填入浏览器，不点击最终发布按钮"
                                    color: publishPage.realPublishEnabled ? "#ffb0b0" : window.muted
                                    font.pixelSize: 9
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 44; radius: 7; color: "#2d291e"; border.color: "#8a6521"
                                    RowLayout { anchors.fill: parent; anchors.leftMargin: 9; anchors.rightMargin: 9; spacing: 7
                                        Text { text: "ⓘ"; color: window.amber; font.pixelSize: 15 }
                                        Text { text: publishPage.realPublishEnabled ? "真实发布已开启：仅对已批准/待发布内容执行最终发布按钮" : "当前为模拟模式，不会真实发布"; color: publishPage.realPublishEnabled ? "#ffb0b0" : window.amber; Layout.fillWidth: true; wrapMode: Text.WordWrap; font.pixelSize: 10 }
                                    }
                                }
                                }
                            }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true; Layout.preferredHeight: 112
                        radius: 10; color: window.panel; border.color: window.line
                        ColumnLayout { anchors.fill: parent; anchors.margins: 12; spacing: 8
                            RowLayout { Layout.fillWidth: true
                                Text { text: "发布队列"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium }
                                Text { text: "按平台版本独立排队"; color: window.muted; font.pixelSize: 10; Layout.leftMargin: 7 }
                                Item { Layout.fillWidth: true }
                                Text { text: "最近更新：" + publishPage.displayTime((publishPage.selectedRow() || {}).updated_at); color: window.muted; font.pixelSize: 9 }
                            }
                            RowLayout { Layout.fillWidth: true; spacing: 8
                                Repeater {
                                    model: [
                                        {label: "待审核", key: "review", color: window.amber, icon: "◷"},
                                        {label: "待发布", key: "queued", color: window.blue, icon: "➤"},
                                        {label: "已发布", key: "published", color: window.green, icon: "✓"},
                                        {label: "失败", key: "failed", color: "#ee7182", icon: "×"}
                                    ]
                                    delegate: Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 45; radius: 7; color: "#111d30"; border.color: window.line
                                        RowLayout { anchors.fill: parent; anchors.leftMargin: 10; anchors.rightMargin: 10; spacing: 8
                                            ColumnLayout { Layout.fillWidth: true; spacing: 1
                                                Text { text: modelData.label; color: window.muted; font.pixelSize: 9 }
                                                Text { text: modelData.key === "failed" ? publishPage.failedCount() : publishPage.statusCount(modelData.key); color: modelData.color; font.pixelSize: 17; font.weight: Font.DemiBold }
                                            }
                                            Text { text: modelData.icon; color: modelData.color; font.pixelSize: 21 }
                                        }
                                    }
                                }
                            }
                            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 28; radius: 6; color: "#111d30"; border.color: window.line
                                RowLayout { anchors.fill: parent; anchors.leftMargin: 9; anchors.rightMargin: 9; spacing: 7
                                    Loader { Layout.preferredWidth: 20; Layout.preferredHeight: 20; sourceComponent: userPlatformIconComponent; onLoaded: item.platform = publishPage.selectedPlatform }
                                    Text { text: publishPage.selectedDraftId > 0 ? ((publishPage.selectedRow() || {}).title || "未命名内容") + " · " + publishPage.platformLabel(publishPage.selectedPlatform) : "请选择内容查看队列状态"; color: window.ink; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 9 }
                                    Text { text: publishPage.selectedDraftId > 0 ? ((publishPage.selectedRow() || {}).status_label || "草稿") : ""; color: publishPage.statusColor((publishPage.selectedRow() || {}).status); font.pixelSize: 9 }
                                    Text { text: "›"; color: window.muted; font.pixelSize: 16 }
                                }
                            }
                        }
                    }
                }
            }
            Page {
                id: analyticsPage
                background: Rectangle { color: window.color }
                property var summary: JSON.parse(backend.snapshotJson || "{}")
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 30
                    spacing: 16
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "数据分析"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                        Text { text: "采集到回复的全链路数据概览"; color: window.muted; font.pixelSize: 12; Layout.leftMargin: 10 }
                        Item { Layout.fillWidth: true }
                        AppButton {
                            text: "刷新数据"
                            onClicked: backend.refresh()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: window.line }
                        }
                    }
                    Text { text: "按任务和平台查看采集、线索与互动转化；详细原文仍保留在对应业务页面"; color: window.muted; font.pixelSize: 12 }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 5
                        columnSpacing: 12
                        Repeater {
                            model: [
                                {label: "采集作品", key: "videos", color: window.blue},
                                {label: "采集评论", key: "comments", color: window.green},
                                {label: "线索总数", key: "leads", color: "#a991ff"},
                                {label: "待互动", key: "interactions", color: window.amber},
                                {label: "运行任务", key: "running_tasks", color: "#f58ca8"}
                            ]
                            delegate: Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 108
                                radius: 12
                                color: window.panel
                                border.color: window.line
                                Column {
                                    anchors.left: parent.left; anchors.top: parent.top; anchors.leftMargin: 16; anchors.topMargin: 16; spacing: 9
                                    Text { text: modelData.label; color: window.muted; font.pixelSize: 11 }
                                    Text { text: analyticsPage.summary[modelData.key] || 0; color: modelData.color; font.pixelSize: 25; font.weight: Font.DemiBold }
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 14
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            radius: 12
                            color: window.panel
                            border.color: window.line
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 18
                                spacing: 13
                                Text { text: "转化漏斗"; color: window.ink; font.pixelSize: 16; font.weight: Font.Medium }
                                Text { text: "作品 → 评论 → 线索 → 互动"; color: window.muted; font.pixelSize: 11 }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    spacing: 9
                                    Repeater {
                                        model: [
                                            {label: "作品", key: "videos", color: window.blue},
                                            {label: "评论", key: "comments", color: window.green},
                                            {label: "线索", key: "leads", color: "#a991ff"},
                                            {label: "互动", key: "interactions", color: window.amber}
                                        ]
                                        delegate: RowLayout {
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 38
                                            Text { text: modelData.label; color: window.muted; Layout.preferredWidth: 42; font.pixelSize: 11 }
                                            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 10; radius: 5; color: "#0d1727"; Rectangle { width: parent.width * Math.min(1, Number(analyticsPage.summary[modelData.key] || 0) / Math.max(1, Number(analyticsPage.summary.videos || 1))); height: parent.height; radius: 5; color: modelData.color } }
                                            Text { text: analyticsPage.summary[modelData.key] || 0; color: window.ink; Layout.preferredWidth: 58; horizontalAlignment: Text.AlignRight; font.pixelSize: 12 }
                                        }
                                    }
                                }
                            }
                        }
                        Rectangle {
                            Layout.preferredWidth: 330
                            Layout.fillHeight: true
                            radius: 12
                            color: window.panel
                            border.color: window.line
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 18
                                spacing: 13
                                Text { text: "平台概览"; color: window.ink; font.pixelSize: 16; font.weight: Font.Medium }
                                Text { text: "六个平台统一接入，任务数据实时汇总"; color: window.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                Repeater {
                                    model: ["抖音", "小红书", "B站", "微博", "快手"]
                                    delegate: Rectangle {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 42
                                        radius: 8
                                        color: index % 2 === 0 ? "#111d30" : "transparent"
                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 12
                                            anchors.rightMargin: 12
                                            Text { text: modelData; color: window.ink; Layout.fillWidth: true; font.pixelSize: 12 }
                                            Text { text: "已接入"; color: window.green; font.pixelSize: 11 }
                                        }
                                    }
                                }
                                Item { Layout.fillHeight: true }
                                Text { text: "数据更新时间跟随后台状态推送"; color: window.muted; font.pixelSize: 10 }
                            }
                        }
                    }
                }
            }
            Page {
                id: diagnosticsPage
                background: Rectangle { color: window.color }
                Component.onCompleted: backend.refreshDiagnostics()
                Flickable {
                    id: diagnosticsScroll
                    anchors.fill: parent
                    clip: true
                    contentWidth: width
                    contentHeight: diagnosticsColumn.implicitHeight + 52
                    ColumnLayout {
                        id: diagnosticsColumn
                        x: 30
                        y: 26
                        width: Math.max(0, diagnosticsScroll.width - 60)
                    spacing: 14
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "诊断与设置"; color: window.ink; font.pixelSize: 24; font.weight: Font.Medium }
                        Text { text: backend.diagnosticCheckedAt ? "最近检查：" + backend.diagnosticCheckedAt : "尚未检查"; color: window.muted; font.pixelSize: 11; Layout.leftMargin: 8 }
                        Item { Layout.fillWidth: true }
                        AppButton {
                            text: "运行五平台检查"
                            onClicked: backend.runPlatformHealth()
                            contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: window.blue; border.color: window.line }
                        }
                        AppButton {
                            text: "检测端口"
                            onClicked: backend.inspectBitBrowser()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "真实浏览器诊断"
                            onClicked: backend.runLiveDiagnostics()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "导出日志"
                            onClicked: backend.exportOperationLog()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: "日志统计"
                            onClicked: logStatsDialog.open()
                            contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: "transparent"; border.color: parent.hovered ? window.blue : "#365174" }
                        }
                        AppButton {
                            text: "刷新诊断"
                            onClicked: backend.refreshDiagnostics()
                            contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: window.panel2; border.color: window.line }
                        }
                        AppButton {
                            text: backend.updateStatus.checking ? "检查中…" : "检查更新"
                            enabled: !backend.updateStatus.checking && !backend.updateStatus.updating
                            onClicked: backend.checkForUpdates()
                            contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: parent.enabled && parent.hovered ? window.panel2 : "transparent"; border.color: parent.enabled && parent.hovered ? window.blue : window.line }
                        }
                        AppButton {
                            visible: Boolean(backend.updateStatus.available)
                            text: "立即更新并重启"
                            enabled: !backend.updateStatus.updating
                            onClicked: backend.installUpdate()
                            contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                            background: Rectangle { radius: 8; color: parent.enabled ? window.blue : window.panel2; border.color: window.line }
                        }
                    }
                    Text {
                        text: {
                            var status = backend.updateStatus || {}
                            var detail = status.notes ? " · " + status.notes : ""
                            return (status.message || "尚未检查更新") + detail
                        }
                        color: backend.updateStatus.available ? window.green : (backend.updateStatus.message && backend.updateStatus.message.indexOf("失败") >= 0 ? "#ff9b9b" : window.muted)
                        wrapMode: Text.WordWrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        font.pixelSize: 10
                    }
                    Text { text: backend.diagnosticExportPath ? "日志已导出：" + backend.diagnosticExportPath : "只读查看连接、账号和健康状态；不会因为刷新诊断而打开、关闭或操作浏览器"; color: backend.diagnosticExportPath ? window.green : window.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 136
                        Rectangle {
                            Layout.fillWidth: true; Layout.fillHeight: true; radius: 12; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 7
                                Text { text: "BitBrowser 连接"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                 Text { text: backend.diagnosticBitBrowser.configured ? "已配置本地服务" : "未配置本地服务"; color: backend.diagnosticBitBrowser.configured ? window.green : window.amber; font.pixelSize: 12 }
                                 Text { text: backend.diagnosticBitBrowser.configured ? "检测超时：" + (backend.diagnosticBitBrowser.timeout || 20) + " 秒" : "请在设置中填写本地 API 地址"; color: window.muted; font.pixelSize: 11 }
                                 Text { text: backend.diagnosticBitBrowserChecks.length ? "检测项：" + backend.diagnosticBitBrowserChecks.map(function(item) { return item.name + "·" + item.status }).join("  ") : "尚未执行端口检测"; color: window.muted; font.pixelSize: 10; elide: Text.ElideRight; Layout.fillWidth: true }
                                 Text { text: backend.diagnosticBitBrowserWindows.length ? "已读取 " + backend.diagnosticBitBrowserWindows.length + " 个浏览器窗口" : ""; color: window.blue; font.pixelSize: 10 }
                            }
                        }
                        Rectangle {
                            Layout.fillWidth: true; Layout.fillHeight: true; radius: 12; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 7
                                Text { text: "智能回复与评论分析 API"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                Text { text: backend.diagnosticLlmApi.enabled ? "已启用配置" : "未启用配置"; color: backend.diagnosticLlmApi.enabled ? window.green : window.muted; font.pixelSize: 12 }
                                Text { text: backend.diagnosticLlmApi.configured ? ((backend.diagnosticLlmApi.provider || "服务商") + " · " + (backend.diagnosticLlmApi.model || "未指定模型")) : "地址或密钥未完整配置"; color: window.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                            }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 150
                        radius: 12; color: window.panel; border.color: window.line
                        ColumnLayout {
                            anchors.fill: parent; anchors.margins: 16; spacing: 8
                            RowLayout { Layout.fillWidth: true
                                Text { text: "员工数据同步"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                Text { text: "仅手动点击同步，不会自动上传"; color: window.muted; font.pixelSize: 10; Layout.leftMargin: 8 }
                                Item { Layout.fillWidth: true }
                                Text { text: "待同步 " + (backend.syncStatus.pending || 0) + " 条"; color: (backend.syncStatus.pending || 0) > 0 ? window.amber : window.muted; font.pixelSize: 11 }
                            }
                            RowLayout { Layout.fillWidth: true; spacing: 8
                                CheckBox {
                                    id: syncEnabledCheck
                                    text: "启用同步"
                                    checked: Boolean(backend.syncStatus.enabled)
                                    spacing: 8; leftPadding: 24; implicitWidth: 92; Layout.preferredWidth: 92
                                    contentItem: Text { text: syncEnabledCheck.text; color: syncEnabledCheck.checked ? window.blue : window.muted; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                    indicator: Rectangle { implicitWidth: 16; implicitHeight: 16; x: 0; y: (syncEnabledCheck.height - height) / 2; radius: 4; color: syncEnabledCheck.checked ? window.blue : "transparent"; border.color: syncEnabledCheck.checked ? window.blue : "#7187a3"; Text { anchors.centerIn: parent; text: "✓"; visible: syncEnabledCheck.checked; color: "#071224"; font.pixelSize: 11 } }
                                }
                                TextField { id: syncServerUrlField; Layout.fillWidth: true; text: backend.syncStatus.server_url || ""; color: window.ink; palette.placeholderText: window.muted; placeholderText: "同步服务器地址，例如 https://server.example.com"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                                TextField { id: syncDeviceNameField; Layout.preferredWidth: 150; text: backend.syncStatus.device_name || ""; color: window.ink; palette.placeholderText: window.muted; placeholderText: "设备名称"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                            }
                            RowLayout { Layout.fillWidth: true; spacing: 8
                                TextField { id: syncTokenField; Layout.fillWidth: true; echoMode: TextInput.Password; color: window.ink; palette.placeholderText: window.muted; placeholderText: backend.syncStatus.api_token_configured ? "留空保留已保存令牌" : "服务器令牌（可选）"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                                AppButton {
                                    text: "保存同步设置"
                                    Layout.preferredWidth: 112
                                    Layout.preferredHeight: 30
                                    onClicked: backend.saveSyncSettings(
                                        syncEnabledCheck.checked,
                                        syncServerUrlField.text,
                                        syncDeviceNameField.text,
                                        syncTokenField.text
                                    )
                                    contentItem: Text {
                                        text: parent.text
                                        color: "#071224"
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                        font.pixelSize: 10
                                    }
                                    background: Rectangle {
                                        radius: 7
                                        color: window.blue
                                        border.color: window.line
                                    }
                                }
                                AppButton {
                                    text: "立即同步"
                                    Layout.preferredWidth: 88
                                    Layout.preferredHeight: 30
                                    enabled: Boolean(backend.syncStatus.enabled)
                                    onClicked: backend.syncNow()
                                    contentItem: Text {
                                        text: parent.text
                                        color: parent.enabled ? "#071224" : window.muted
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                        font.pixelSize: 10
                                    }
                                    background: Rectangle {
                                        radius: 7
                                        color: parent.enabled ? window.blue : window.panel2
                                        border.color: window.line
                                    }
                                }
                            }
                            Text { text: backend.syncStatus.status === "failed" || backend.syncStatus.status === "error" ? ("同步失败：" + (backend.syncStatus.last_error || "未知错误")) : backend.syncStatus.status === "success" ? ("最近同步完成：" + (backend.syncStatus.last_sync_at || "")) : "员工数据会按登录账号隔离；历史未归属数据不会被员工自动认领"; color: backend.syncStatus.status === "failed" || backend.syncStatus.status === "error" ? "#ff9b9b" : window.muted; font.pixelSize: 10; elide: Text.ElideRight; Layout.fillWidth: true }
                        }
                    }
                    Rectangle {
                        // 贴吧功能暂时保留在代码中，但不在当前版本界面启用。
                        visible: false
                        Layout.fillWidth: true
                        Layout.preferredHeight: 0
                        Layout.minimumHeight: 0
                        radius: 12; color: window.panel; border.color: window.line
                        ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 7
                            RowLayout { Layout.fillWidth: true
                                Text { text: "百度贴吧 API"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                Text { text: backend.diagnosticTiebaApi.enabled ? "已启用" : "未启用"; color: backend.diagnosticTiebaApi.enabled ? window.green : window.muted; font.pixelSize: 11; Layout.leftMargin: 8 }
                                Item { Layout.fillWidth: true }
                                Text { text: backend.diagnosticTiebaApi.configured ? "令牌已配置" : "令牌未配置"; color: backend.diagnosticTiebaApi.configured ? window.green : window.amber; font.pixelSize: 11 }
                            }
                            RowLayout { Layout.fillWidth: true; spacing: 8
                                Text { text: "TB_TOKEN"; color: window.muted; Layout.preferredWidth: 76; font.pixelSize: 11 }
                                TextField { id: tiebaTokenField; Layout.fillWidth: true; echoMode: TextInput.Password; color: window.ink; palette.placeholderText: window.muted; placeholderText: backend.diagnosticTiebaApi.token_configured ? "留空保留已保存令牌" : "粘贴贴吧认证令牌"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                                CheckBox { id: tiebaEnabledCheck; text: "启用"; checked: Boolean(backend.diagnosticTiebaApi.enabled); spacing: 8; leftPadding: 24; implicitWidth: 68; contentItem: Text { text: tiebaEnabledCheck.text; color: tiebaEnabledCheck.checked ? window.blue : window.muted; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } indicator: Rectangle { implicitWidth: 16; implicitHeight: 16; x: 0; y: (tiebaEnabledCheck.height-height)/2; radius: 4; color: tiebaEnabledCheck.checked ? window.blue : "transparent"; border.color: tiebaEnabledCheck.checked ? window.blue : "#7187a3"; Text { anchors.centerIn: parent; text: "✓"; visible: tiebaEnabledCheck.checked; color: "#071224"; font.pixelSize: 11 } } }
                                AppButton { text: "保存"; Layout.preferredWidth: 68; onClicked: backend.saveTiebaSettings(tiebaTokenField.text, tiebaEnabledCheck.checked); contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line } }
                            }
                            Text { text: backend.diagnosticTiebaApi.configured ? "已连接官方贴吧接口，可创建贴吧采集任务" : "尚未配置 TB_TOKEN；未配置时贴吧任务不会启动"; color: backend.diagnosticTiebaApi.configured ? window.green : window.muted; font.pixelSize: 10; elide: Text.ElideRight; Layout.fillWidth: true }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 176
                        radius: 12; color: window.panel; border.color: window.line
                        GridLayout {
                            anchors.fill: parent
                            anchors.margins: 16
                            columns: 4
                            columnSpacing: 10
                            rowSpacing: 8
                            Text { text: "BitBrowser 地址"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                            TextField { id: bitbrowserUrlField; Layout.fillWidth: true; Layout.columnSpan: 2; text: backend.diagnosticBitBrowser.base_url || "http://127.0.0.1:54345"; color: window.ink; palette.placeholderText: window.muted; placeholderText: "http://127.0.0.1:54345"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                            TextField { id: bitbrowserTimeoutField; Layout.preferredWidth: 72; text: String(backend.diagnosticBitBrowser.timeout || 20); color: window.ink; palette.placeholderText: window.muted; placeholderText: "秒"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                            Text { text: "服务商"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                            TextField { id: apiProviderField; Layout.fillWidth: true; text: backend.diagnosticLlmApi.provider || ""; color: window.ink; palette.placeholderText: window.muted; placeholderText: "服务商"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                            TextField { id: apiUrlField; Layout.fillWidth: true; text: backend.diagnosticLlmApi.base_url || ""; color: window.ink; palette.placeholderText: window.muted; placeholderText: "智能 API 地址"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                            TextField { id: apiModelField; Layout.fillWidth: true; text: backend.diagnosticLlmApi.model || ""; color: window.ink; palette.placeholderText: window.muted; placeholderText: "模型名称"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                            Text { text: "API Key"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                            TextField { id: apiKeyField; Layout.fillWidth: true; Layout.columnSpan: 2; echoMode: TextInput.Password; color: window.ink; palette.placeholderText: window.muted; placeholderText: "留空表示保留已保存密钥"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                            RowLayout { Layout.fillWidth: true; spacing: 8
                                CheckBox {
                                    id: apiEnabledCheck
                                    text: "启用 API"
                                    checked: Boolean(backend.diagnosticLlmApi.enabled)
                                    spacing: 8
                                    leftPadding: 24
                                    implicitWidth: 92
                                    Layout.preferredWidth: 92
                                    Layout.minimumWidth: 92
                                    contentItem: Text { text: apiEnabledCheck.text; color: apiEnabledCheck.checked ? window.blue : window.muted; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                    indicator: Rectangle {
                                        implicitWidth: 16; implicitHeight: 16
                                        x: 0; y: (apiEnabledCheck.height - height) / 2
                                        radius: 4
                                        color: apiEnabledCheck.checked ? window.blue : "transparent"
                                        border.color: apiEnabledCheck.checked ? window.blue : "#7187a3"
                                        Text { anchors.centerIn: parent; text: "✓"; visible: apiEnabledCheck.checked; color: "#071224"; font.pixelSize: 11 }
                                    }
                                }
                                AppButton { text: "保存设置"; onClicked: backend.saveSettings(bitbrowserUrlField.text, parseInt(bitbrowserTimeoutField.text || "20"), apiProviderField.text, apiUrlField.text, apiKeyField.text, apiModelField.text, apiEnabledCheck.checked); contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 7; color: window.blue; border.color: window.line } }
                            }
                        }
                    }
                    Rectangle {
                        visible: backend.diagnosticBitBrowserChecks.length > 0 || backend.diagnosticBitBrowserWindows.length > 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: 178
                        radius: 12; color: window.panel; border.color: window.line
                        RowLayout { anchors.fill: parent; anchors.margins: 16; spacing: 18
                            ColumnLayout {
                                Layout.fillWidth: true; Layout.fillHeight: true; spacing: 6
                                Text { text: "端口检测明细"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium }
                                ListView {
                                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 1
                                    model: backend.diagnosticBitBrowserChecks
                                    delegate: Text { width: ListView.view.width; height: 28; text: modelData.name + "：" + modelData.status + (modelData.detail ? " · " + modelData.detail : ""); color: modelData.status === "正常" ? window.green : window.amber; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true; Layout.fillHeight: true; spacing: 6
                                Text { text: "浏览器窗口与调试端口"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium }
                                ListView {
                                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 1
                                    model: backend.diagnosticBitBrowserWindows
                                    delegate: Text { width: ListView.view.width; height: 28; text: (modelData.name || "未命名窗口") + " · " + (modelData.port || "未识别端口") + (modelData.opened ? " · 已打开" : " · 未打开"); color: modelData.opened ? window.green : window.muted; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                }
                            }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 208
                        radius: 12; color: window.panel; border.color: window.line
                        ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 8
                            Text { text: "五平台健康状态"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                            ListView {
                                Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 1
                                model: backend.diagnosticHealthRows
                                delegate: Rectangle {
                                    width: ListView.view.width; height: 34; color: index % 2 === 0 ? "transparent" : "#17273d"
                                    RowLayout { anchors.fill: parent; anchors.leftMargin: 8; anchors.rightMargin: 8; spacing: 12
                                        Text { text: modelData.platform_label; color: window.blue; Layout.preferredWidth: 72; font.pixelSize: 12 }
                                        Text { text: modelData.status_label || "未检查"; color: modelData.status === "ok" ? window.green : window.amber; Layout.preferredWidth: 80; font.pixelSize: 12 }
                                        Text { text: modelData.detail || "暂无检查记录"; color: window.muted; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 11 }
                                        Text { text: modelData.accounts !== undefined ? (modelData.accounts + " 个账号") : ""; color: window.muted; Layout.preferredWidth: 72; font.pixelSize: 11 }
                                    }
                                }
                            }
                        }
                    }
                    Rectangle {
                        visible: backend.diagnosticLiveHealthRows.length > 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: 190
                        radius: 12; color: window.panel; border.color: window.line
                        ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 8
                            Text { text: "真实浏览器诊断结果（只读）"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                            ListView {
                                Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 1
                                model: backend.diagnosticLiveHealthRows
                                delegate: Rectangle {
                                    width: ListView.view.width; height: 46; color: index % 2 === 0 ? "transparent" : "#17273d"
                                    RowLayout { anchors.fill: parent; anchors.leftMargin: 8; anchors.rightMargin: 8; spacing: 12
                                        Text { text: modelData.platform_label; color: window.blue; Layout.preferredWidth: 72; font.pixelSize: 12 }
                                        Text { text: modelData.status_label || "未检查"; color: modelData.status === "ok" ? window.green : window.amber; Layout.preferredWidth: 64; font.pixelSize: 12 }
                                        Text { text: modelData.detail || "暂无诊断说明"; color: window.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideRight; font.pixelSize: 10 }
                                        Text { text: modelData.accounts !== undefined ? modelData.accounts + " 个账号" : ""; color: window.muted; Layout.preferredWidth: 72; font.pixelSize: 10 }
                                    }
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.preferredHeight: 220; spacing: 14
                        Rectangle {
                            Layout.fillWidth: true; Layout.fillHeight: true; radius: 12; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 8
                                Text { text: "平台账号概览"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                ListView {
                                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 1
                                    model: backend.diagnosticAccountRows
                                    delegate: Rectangle { width: ListView.view.width; height: 34; color: index % 2 === 0 ? "transparent" : "#17273d"; RowLayout { anchors.fill: parent; anchors.leftMargin: 8; anchors.rightMargin: 8; Text { text: modelData.platform_label; color: window.blue; Layout.preferredWidth: 72; font.pixelSize: 11 } Text { text: modelData.accounts + " 个账号"; color: window.ink; Layout.fillWidth: true; font.pixelSize: 11 } Text { text: "已绑定 " + modelData.bound_accounts; color: window.muted; Layout.preferredWidth: 72; font.pixelSize: 11 } Text { text: modelData.waiting_human ? (modelData.waiting_human + " 待处理") : ""; color: window.amber; Layout.preferredWidth: 72; font.pixelSize: 11 } } }
                                }
                            }
                        }
                        Rectangle {
                            Layout.fillWidth: true; Layout.fillHeight: true; radius: 12; color: window.panel; border.color: window.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 8
                                RowLayout { Layout.fillWidth: true
                                    Text { text: "当天运行日志"; color: window.ink; font.pixelSize: 15; font.weight: Font.Medium }
                                    Item { Layout.fillWidth: true }
                                    Text { text: (backend.diagnosticLogStats.total || 0) + " 条"; color: window.muted; font.pixelSize: 10 }
                                }
                                ListView {
                                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 1
                                    model: backend.diagnosticLogs
                                    delegate: Text { width: ListView.view.width; height: 28; text: "[" + String(modelData.timestamp || "").slice(-12) + "] [" + String(modelData.source || "") + "] " + modelData.message + (modelData.details && Object.keys(modelData.details).length ? " · 详情=" + JSON.stringify(modelData.details) : ""); color: modelData.level === "error" ? "#ff9b9b" : (modelData.level === "warning" ? window.amber : window.green); elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Connections {
        target: backend
        function onCurrentPageChanged() {
            var page = String(backend.currentPage || "")
            if (page === "publish") {
                window.workspaceMode = "publish"
            } else if (page !== "") {
                window.workspaceMode = "collect"
                window.lastCollectionPage = page
            }
            if (page !== window.lastAutoInspectedPage) {
                window.lastAutoInspectedPage = page
                if (page === "accounts") {
                    backend.inspectBitBrowser()
                    if (!accountsPage.nicknameRefreshRequested) {
                        accountsPage.nicknameRefreshRequested = true
                        backend.refreshAccountNicknames()
                    }
                }
            }
        }
        function onCommandFinished(command, ok, message) {
            if (String(command || "") === "create_browser_window" && ok) {
                // 创建接口会自动打开窗口；诊断列表是添加账号下拉框的数据源，
                // 必须在后台命令完成后重新读取，避免用户看到旧窗口列表。
                backend.inspectBitBrowser()
            }
            if (String(command || "") === "schedule_publish") {
                publishPage.scheduleNotice = ok
                    ? String(message || "定时发布已保存")
                    : ("定时发布失败：" + String(message || "未知错误"))
                if (ok) publishPage.requestRefresh()
            }
            if (String(command || "") === "save_template") {
                templateDialog.saving = false
                templateDialog.saveStatusOk = ok
                templateDialog.saveStatus = ok
                    ? String(message || "模板已保存")
                    : ("保存失败：" + String(message || "未知错误"))
            }
            if (String(command || "") === "generate_content") {
                publishPage.generationPending = false
                publishPage.generationNotice = ok
                    ? String(message || "内容生成完成")
                    : ("生成失败：" + String(message || "未知错误"))
            }
            if (String(command || "") === "delete_generated_content") {
                publishPage.generationNotice = ok
                    ? String(message || "生成内容已删除")
                    : ("删除失败：" + String(message || "未知错误"))
            }
            if (String(command || "") === "open_published_message_browser") {
                publishPage.messageActionPendingId = 0
                publishPage.messageActionNotice = ok
                    ? String(message || "已打开对应浏览器回复页面")
                    : ("打开失败：" + String(message || "未知错误"))
            }
            if (String(command || "") === "delete_browser_window" && ok) {
                // 删除完成后重新读取窗口列表，立即移除已删除的 profile。
                backend.inspectBitBrowser()
            }
            if (String(command || "") === "delete_task") {
                tasksPage.deletingTaskId = 0
            }
            if (String(command || "") === "interaction_type_switch") {
                var switchMessage = String(message || "")
                var switchParts = switchMessage.split(":")
                var switchLeadId = switchParts.length > 1 ? Number(switchParts[1]) : 0
                interactionPage2.finishTypeSwitch(switchLeadId)
                if (ok && switchParts.length >= 2) {
                    interactionPage2.activeType = switchParts[0]
                    interactionPage2.selectedDraftIds = []
                    interactionPage2.requestInteractionRefresh()
                }
            }
            if (String(command || "") === "interaction_action" && ok
                    && String(message || "").indexOf("enter_send:") === 0) {
                // 后端已完成 draft -> queued；这里同步切换到待发送页，
                // 让用户能立即看到刚刚迁移的内容。
                interactionPage2.activeStatus = "queued"
                interactionPage2.selectedDraftIds = []
                interactionPage2.requestInteractionRefresh()
            }
            if (String(command || "") === "interaction_action") {
                var actionMessage = String(message || "")
                var actionParts = actionMessage.split(":")
                var actionDraftId = 0
                if (ok && actionParts.length > 1)
                    actionDraftId = Number(actionParts[actionParts.length - 1])
                else if (!ok && actionParts.length > 1 && actionParts[0] === "error")
                    actionDraftId = Number(actionParts[1])
                interactionPage2.finishAction(actionDraftId)
                if (ok) {
                    var actionName = actionParts.length > 0 ? actionParts[0] : ""
                    // 状态迁移后必须切到目标页签再刷新。否则“退回待生成”
                    // 仍按 queued 查询，页面会看起来空白；账号分配、编辑、
                    // 删除等操作则按用户当前所在页签刷新。
                    if (actionName === "return_queued" || actionName === "return_failed") {
                        interactionPage2.activeStatus = "draft"
                        interactionPage2.selectedDraftIds = []
                    }
                    if (actionName !== "enter_send")
                        interactionPage2.requestInteractionRefresh()
                }
            }
            if (String(command || "") === "send_interactions") {
                interactionPage2.pendingSendDraftIds = []
                if (ok && interactionPage2.realSendEnabled) {
                    // 真实发送成功后直接进入“已回复”，让刚完成的记录可立即回访；
                    // 模拟填充仍留在待发送池，便于继续测试，不伪造发送结果。
                    interactionPage2.activeStatus = "sent"
                    interactionPage2.selectedDraftIds = []
                    interactionPage2.requestInteractionRefresh()
                }
            }
        }
    }

    // 账户认证覆盖层：认证成功前不加载任何业务操作入口，避免未审批员工
    // 看到或调用其他员工的本地数据。注册只写入待审批账户，不自动登录。
    // 这里与已确认的登录设计稿保持同一结构：左侧品牌/能力引导，右侧认证卡片。
    Rectangle {
        id: authOverlay
        anchors.fill: parent
        z: 1000
        visible: !backend.authenticated
        color: "#03142d"
        clip: true
        property bool registerMode: false
        property bool showPassword: false
        function submit() {
            if (!authPrimaryButton.enabled) return
            if (authOverlay.registerMode)
                backend.registerUser(authUsernameField.text, authPasswordField.text, authEmployeeField.text)
            else
                backend.loginUser(authUsernameField.text, authPasswordField.text, authRememberBox.checked)
        }

        Rectangle {
            anchors.fill: parent
            opacity: 0.8
            gradient: Gradient {
                GradientStop { position: 0.0; color: "#061a39" }
                GradientStop { position: 0.52; color: "#03132b" }
                GradientStop { position: 1.0; color: "#020d20" }
            }
        }

        // 设计稿中的关闭入口，实际关闭窗口，不触碰认证和业务数据。
        AppButton {
            id: authCloseButton
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.topMargin: 18
            anchors.rightMargin: 22
            width: 36
            height: 36
            text: "×"
            z: 5
            onClicked: Qt.quit()
            contentItem: Text {
                text: parent.text
                color: parent.hovered ? window.ink : "#d9e6f7"
                font.pixelSize: 30
                font.weight: Font.Light
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle { radius: 8; color: parent.hovered ? "#19365c" : "transparent" }
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 56
            anchors.rightMargin: 74
            anchors.topMargin: 28
            anchors.bottomMargin: 28
            spacing: 44

            Item {
                id: authHero
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 440
                Layout.preferredWidth: authOverlay.width * 0.54

                Column {
                    anchors.left: parent.left
                    anchors.leftMargin: Math.max(18, parent.width * 0.12)
                    anchors.top: parent.top
                    anchors.topMargin: Math.max(44, parent.height * 0.14)
                    width: Math.min(620, parent.width * 0.82)
                    spacing: 12
                    Text {
                        text: "多平台采集工作台"
                        color: window.ink
                        font.pixelSize: Math.min(44, Math.max(32, authHero.width * 0.045))
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: "统一管理采集、互动与发布"
                        color: "#a9b9d0"
                        font.pixelSize: 22
                        font.weight: Font.Light
                    }
                    Rectangle { width: 160; height: 2; radius: 1; color: "#238dff"; opacity: 0.85; anchors.leftMargin: 2 }
                }

                Item {
                    id: authNetwork
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    anchors.topMargin: parent.height * 0.39
                    anchors.bottomMargin: parent.height * 0.02

                    Canvas {
                        anchors.fill: parent
                        onPaint: {
                            var ctx = getContext("2d")
                            ctx.clearRect(0, 0, width, height)
                            ctx.lineCap = "round"
                            var centerX = width * 0.51
                            var centerY = height * 0.59
                            var starts = [0.04, 0.17, 0.78, 0.91]
                            for (var i = 0; i < starts.length; i++) {
                                var left = i < 2
                                ctx.beginPath()
                                ctx.moveTo(width * starts[i], height * (0.33 + (i % 2) * 0.24))
                                ctx.bezierCurveTo(width * (left ? 0.25 : 0.75), height * (0.28 + (i % 2) * 0.2),
                                                 width * (left ? 0.38 : 0.64), height * 0.56, centerX, centerY)
                                ctx.strokeStyle = i % 2 === 0 ? "#1d79e8" : "#0cd5ff"
                                ctx.globalAlpha = 0.62
                                ctx.lineWidth = 1.4
                                ctx.stroke()
                            }
                            ctx.globalAlpha = 0.22
                            ctx.strokeStyle = "#3fa5ff"
                            ctx.lineWidth = 1
                            for (var ring = 1; ring <= 3; ring++) {
                                ctx.beginPath()
                                ctx.ellipse(centerX, centerY + 30, width * (0.11 + ring * 0.08), 17 + ring * 12, 0, 0, Math.PI * 2)
                                ctx.stroke()
                            }
                            ctx.globalAlpha = 1
                        }
                    }

                    Repeater {
                        model: [
                            {icon: "▶", x: 0.08, y: 0.04},
                            {icon: "☁", x: 0.82, y: 0.05},
                            {icon: "☷", x: 0.04, y: 0.45},
                            {icon: "◔", x: 0.85, y: 0.46},
                            {icon: "▥", x: 0.09, y: 0.80},
                            {icon: "▤", x: 0.82, y: 0.79}
                        ]
                        delegate: Rectangle {
                            property var cardData: modelData
                            x: authNetwork.width * cardData.x
                            y: authNetwork.height * cardData.y
                            width: 62
                            height: 62
                            radius: 9
                            color: "#0b2b50"
                            border.color: "#2a83d8"
                            border.width: 1
                            opacity: 0.93
                            Text {
                                anchors.centerIn: parent
                                text: cardData.icon
                                color: "#cce9ff"
                                font.pixelSize: 29
                                font.weight: Font.Light
                            }
                        }
                    }

                    Rectangle {
                        id: authCoreGlow
                        anchors.horizontalCenter: parent.horizontalCenter
                        y: parent.height * 0.45
                        width: 150
                        height: 92
                        radius: 46
                        color: "#1477ed"
                        opacity: 0.16
                    }
                    Rectangle {
                        id: authCoreBase
                        anchors.horizontalCenter: parent.horizontalCenter
                        y: parent.height * 0.68
                        width: 124
                        height: 20
                        radius: 10
                        color: "#0b5bd0"
                        border.color: "#5ccfff"
                        opacity: 0.9
                    }
                    Repeater {
                        model: 3
                        delegate: Rectangle {
                            anchors.horizontalCenter: parent.horizontalCenter
                            y: authNetwork.height * 0.53 - index * 22
                            width: 58 + index * 12
                            height: 42
                            rotation: 30
                            color: "#123b68"
                            border.color: index === 1 ? "#5cd6ff" : "#328be7"
                            opacity: 0.88
                        }
                    }
                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter
                        y: authNetwork.height * 0.20
                        width: 3
                        height: authNetwork.height * 0.30
                        color: "#58d8ff"
                        opacity: 0.8
                    }
                }
            }

            Rectangle {
                id: authCard
                Layout.alignment: Qt.AlignVCenter
                Layout.minimumWidth: 440
                Layout.maximumWidth: 560
                Layout.preferredWidth: Math.min(520, authOverlay.width * 0.40)
                Layout.preferredHeight: Math.min(authOverlay.registerMode ? 700 : 650, authOverlay.height - 54)
                radius: 17
                color: "#102541"
                border.color: "#435b7b"
                border.width: 1
                opacity: 0.98

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 44
                    spacing: 12

                    Text {
                        Layout.fillWidth: true
                        text: "欢迎回来"
                        color: window.ink
                        font.pixelSize: 34
                        font.weight: Font.DemiBold
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Text {
                        Layout.fillWidth: true
                        text: authOverlay.registerMode ? "提交申请后等待管理员审批" : "登录后继续使用工作台"
                        color: "#b3c0d4"
                        font.pixelSize: 17
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Item { Layout.preferredHeight: 10 }

                    Text { text: "账号"; color: window.ink; font.pixelSize: 16 }
                    TextField {
                        id: authUsernameField
                        Layout.fillWidth: true
                        Layout.preferredHeight: 54
                        color: window.ink
                        leftPadding: 48
                        placeholderText: "请输入账号"
                        placeholderTextColor: "#7d92ad"
                        selectByMouse: true
                        font.pixelSize: 14
                        background: Rectangle {
                            radius: 9
                            color: "#081a31"
                            border.color: parent.activeFocus ? window.blue : "#49627f"
                            border.width: parent.activeFocus ? 2 : 1
                            Text { anchors.left: parent.left; anchors.leftMargin: 15; anchors.verticalCenter: parent.verticalCenter; text: "♙"; color: "#a9bad0"; font.pixelSize: 25 }
                        }
                    }

                    Text { visible: authOverlay.registerMode; text: "员工姓名"; color: window.ink; font.pixelSize: 16 }
                    TextField {
                        id: authEmployeeField
                        visible: authOverlay.registerMode
                        Layout.fillWidth: true
                        Layout.preferredHeight: 54
                        color: window.ink
                        leftPadding: 48
                        placeholderText: "请输入真实姓名或称呼"
                        placeholderTextColor: "#7d92ad"
                        selectByMouse: true
                        font.pixelSize: 14
                        background: Rectangle {
                            radius: 9
                            color: "#081a31"
                            border.color: parent.activeFocus ? window.blue : "#49627f"
                            border.width: parent.activeFocus ? 2 : 1
                            Text { anchors.left: parent.left; anchors.leftMargin: 15; anchors.verticalCenter: parent.verticalCenter; text: "♙"; color: "#a9bad0"; font.pixelSize: 25 }
                        }
                    }

                    Text { text: "密码"; color: window.ink; font.pixelSize: 16 }
                    TextField {
                        id: authPasswordField
                        Layout.fillWidth: true
                        Layout.preferredHeight: 54
                        color: window.ink
                        leftPadding: 48
                        rightPadding: 50
                        placeholderText: authOverlay.registerMode ? "至少 6 位" : "请输入密码"
                        placeholderTextColor: "#7d92ad"
                        echoMode: authOverlay.showPassword ? TextInput.Normal : TextInput.Password
                        selectByMouse: true
                        font.pixelSize: 14
                        onAccepted: authOverlay.submit()
                        background: Rectangle {
                            radius: 9
                            color: "#081a31"
                            border.color: parent.activeFocus ? window.blue : "#49627f"
                            border.width: parent.activeFocus ? 2 : 1
                            Text { anchors.left: parent.left; anchors.leftMargin: 15; anchors.verticalCenter: parent.verticalCenter; text: "▣"; color: "#a9bad0"; font.pixelSize: 21 }
                            AppButton {
                                anchors.right: parent.right
                                anchors.rightMargin: 9
                                anchors.verticalCenter: parent.verticalCenter
                                width: 34
                                height: 34
                                text: authOverlay.showPassword ? "◉" : "◎"
                                onClicked: authOverlay.showPassword = !authOverlay.showPassword
                                contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : "#9db0c8"; font.pixelSize: 21; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                background: Rectangle { radius: 7; color: parent.hovered ? "#173456" : "transparent" }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: 2
                        AppCheckBox { id: authRememberBox; text: "记住登录状态"; checked: false; Layout.fillWidth: true }
                    }
                    Text {
                        visible: backend.authMessage !== ""
                        Layout.fillWidth: true
                        text: backend.authMessage
                        color: backend.authMessage.indexOf("成功") >= 0 ? window.green : window.amber
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                    }
                    Item { Layout.fillHeight: true; Layout.minimumHeight: 5 }

                    AppButton {
                        id: authPrimaryButton
                        Layout.fillWidth: true
                        Layout.preferredHeight: 54
                        enabled: authUsernameField.text.trim().length > 0 && authPasswordField.text.length >= 6 && (!authOverlay.registerMode || authEmployeeField.text.trim().length > 0)
                        text: authOverlay.registerMode ? "提交注册申请" : "登录"
                        onClicked: authOverlay.submit()
                        contentItem: Text { text: parent.text; color: parent.enabled ? "#ffffff" : "#7187a3"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 17; font.weight: Font.DemiBold }
                        background: Rectangle {
                            radius: 9
                            border.color: parent.enabled ? "#4daeff" : "#314963"
                            gradient: Gradient {
                                GradientStop { position: 0.0; color: parent.enabled ? "#299dff" : "#172b45" }
                                GradientStop { position: 1.0; color: parent.enabled ? "#1260d8" : "#112238" }
                            }
                        }
                    }
                    AppButton {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 50
                        text: authOverlay.registerMode ? "已有账号，返回登录" : "注册申请"
                        onClicked: {
                            authOverlay.registerMode = !authOverlay.registerMode
                            authOverlay.showPassword = false
                            authUsernameField.text = ""
                            authEmployeeField.text = ""
                            authPasswordField.text = ""
                        }
                        contentItem: Text { text: parent.text; color: parent.hovered ? "#63bcff" : "#36a5ff"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 17 }
                        background: Rectangle { radius: 9; color: parent.hovered ? "#122f53" : "transparent"; border.color: "#2294ff"; border.width: 1 }
                    }

                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#36506f"; opacity: 0.75; Layout.topMargin: 8 }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 10
                        Rectangle { width: 14; height: 14; radius: 7; color: backend.connectionText === "后台服务已连接" ? "#35d49a" : window.amber }
                        Text { text: backend.connectionText === "后台服务已连接" ? "服务连接正常" : backend.connectionText; color: "#c4d2e4"; font.pixelSize: 16 }
                    }
                }
            }
        }
    }

    Dialog {
        id: personalCenterDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(520, window.width - 56)
        title: "个人中心"
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 50
            color: window.panel2
            border.color: window.line
            Text {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 52
                text: personalCenterDialog.title
                color: window.ink
                verticalAlignment: Text.AlignVCenter
                font.pixelSize: 16
                font.weight: Font.DemiBold
            }
            AppButton {
                anchors.right: parent.right
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                width: 32
                height: 32
                text: "×"
                onClicked: personalCenterDialog.close()
                contentItem: Text {
                    text: parent.text
                    color: parent.hovered ? window.ink : window.muted
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    font.pixelSize: 21
                }
                background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" }
            }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 76
                radius: 10
                color: "#17273d"
                border.color: window.line
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 10
                    Rectangle {
                        Layout.preferredWidth: 42
                        Layout.preferredHeight: 42
                        radius: 21
                        color: "#5f7cff"
                        Text {
                            anchors.centerIn: parent
                            text: String(backend.authEmployeeName || backend.authUsername || "人").slice(0, 1)
                            color: "#ffffff"
                            font.pixelSize: 17
                            font.weight: Font.DemiBold
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 3
                        Text { text: backend.authEmployeeName || "未设置姓名"; color: window.ink; font.pixelSize: 14; font.weight: Font.Medium; elide: Text.ElideRight; Layout.fillWidth: true }
                        Text { text: "账号：" + (backend.authUsername || "未知") + (backend.authIsAdmin ? "  ·  管理员" : "  ·  员工"); color: window.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                    }
                }
            }
            Text { text: "数据同步"; color: window.ink; font.pixelSize: 13; font.weight: Font.Medium }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    text: backend.syncStatus.enabled ? (backend.syncStatus.pending + " 条待上传") : "同步未启用"
                    color: backend.syncStatus.status === "error" || backend.syncStatus.status === "failed" ? "#ff9b9b" : window.muted
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    font.pixelSize: 11
                }
                AppButton {
                    text: "立即上传"
                    enabled: Boolean(backend.syncStatus.enabled)
                    Layout.preferredWidth: 88
                    Layout.preferredHeight: 32
                    onClicked: backend.syncNow()
                    contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                    background: Rectangle { radius: 7; color: parent.enabled ? window.blue : window.panel2; border.color: window.line }
                }
            }
            Text {
                text: backend.syncStatus.status === "error" || backend.syncStatus.status === "failed" ? ("同步失败：" + (backend.syncStatus.last_error || "未知错误")) : (backend.syncStatus.last_sync_at ? "最近上传：" + backend.syncStatus.last_sync_at : "只上传当前登录员工的数据，不会上传浏览器 Cookie 和 API 密钥")
                color: backend.syncStatus.status === "error" || backend.syncStatus.status === "failed" ? "#ff9b9b" : window.muted
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                font.pixelSize: 10
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
            Text { visible: backend.authIsAdmin; text: "管理员功能"; color: window.ink; font.pixelSize: 13; font.weight: Font.Medium }
            RowLayout {
                visible: backend.authIsAdmin
                Layout.fillWidth: true
                spacing: 8
                AppButton {
                    text: "用户审批"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 34
                    onClicked: { personalCenterDialog.close(); backend.refreshAuthUsers(); adminUsersDialog.open() }
                    contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                    background: Rectangle { radius: 7; color: parent.hovered ? "#182748" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                }
                AppButton {
                    text: "管理员中心"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 34
                    onClicked: { personalCenterDialog.close(); backend.refreshAdminDashboard(); adminCenterDialog.open() }
                    contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                    background: Rectangle { radius: 7; color: parent.hovered ? "#182748" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                }
            }
            Item { Layout.fillHeight: true }
            AppButton {
                text: "退出登录"
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                onClicked: { personalCenterDialog.close(); backend.logoutUser() }
                contentItem: Text { text: parent.text; color: "#ffd9df"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12; font.weight: Font.DemiBold }
                background: Rectangle { radius: 8; color: parent.hovered ? "#6b3443" : "#4b2a37"; border.color: "#8b4d5a" }
            }
        }
    }

    Dialog {
        id: adminUsersDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(760, window.width - 60)
        height: Math.min(590, window.height - 70)
        title: "用户审批"
        function statusLabel(value) {
            var labels = {pending: "待审批", approved: "已通过", rejected: "已拒绝", disabled: "已禁用"}
            return labels[String(value || "")] || String(value || "未知")
        }
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 48
            color: window.panel2
            border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 48; text: adminUsersDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 15; font.weight: Font.DemiBold }
            AppButton { anchors.right: parent.right; anchors.rightMargin: 7; anchors.verticalCenter: parent.verticalCenter; width: 32; height: 32; text: "×"; onClicked: adminUsersDialog.close(); contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 } background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" } }
        }
        contentItem: ColumnLayout {
            spacing: 8
            Text { text: "管理员可以审批、拒绝或禁用员工账号。默认管理员：admin"; color: window.muted; font.pixelSize: 11; Layout.fillWidth: true }
            ListView {
                id: adminUserList
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: 6
                model: backend.authUsers
                delegate: Rectangle {
                    width: adminUserList.width
                    height: 62
                    radius: 8
                    color: index % 2 === 0 ? "#17273d" : "#142135"
                    border.color: window.line
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 12
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text { text: modelData.employee_name + "  ·  " + modelData.username; color: window.ink; font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true }
                            Text { text: "创建于 " + (modelData.created_at || "暂无"); color: window.muted; font.pixelSize: 10; elide: Text.ElideRight; Layout.fillWidth: true }
                        }
                        Text { text: adminUsersDialog.statusLabel(modelData.status); color: modelData.status === "approved" ? window.green : (modelData.status === "pending" ? window.amber : window.muted); font.pixelSize: 11; Layout.preferredWidth: 54; horizontalAlignment: Text.AlignHCenter }
                        AppButton { visible: modelData.status === "pending"; text: "通过"; Layout.preferredWidth: 58; Layout.preferredHeight: 30; onClicked: backend.approveAuthUser(Number(modelData.id)); contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 6; color: window.green } }
                        AppButton { visible: modelData.status === "pending"; text: "拒绝"; Layout.preferredWidth: 58; Layout.preferredHeight: 30; onClicked: backend.rejectAuthUser(Number(modelData.id)); contentItem: Text { text: parent.text; color: "#ffd9df"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 6; color: "#5a2f3b"; border.color: "#8b4d5a" } }
                        AppButton { visible: modelData.status === "approved" && modelData.username !== "admin"; text: "禁用"; Layout.preferredWidth: 58; Layout.preferredHeight: 30; onClicked: backend.disableAuthUser(Number(modelData.id)); contentItem: Text { text: parent.text; color: window.amber; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 6; color: "transparent"; border.color: window.line } }
                    }
                }
                Text { anchors.centerIn: parent; visible: adminUserList.count === 0; text: "暂无账号申请"; color: window.muted; font.pixelSize: 12 }
            }
        }
    }

    Dialog {
        id: adminCenterDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(1120, window.width - 50)
        height: Math.min(720, window.height - 50)
        title: "管理员中心"
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 50
            color: window.panel2
            border.color: window.line
            Text {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 52
                text: adminCenterDialog.title
                color: window.ink
                verticalAlignment: Text.AlignVCenter
                font.pixelSize: 16
                font.weight: Font.DemiBold
            }
            AppButton {
                anchors.right: parent.right
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                width: 32
                height: 32
                text: "×"
                onClicked: adminCenterDialog.close()
                contentItem: Text {
                    text: parent.text
                    color: parent.hovered ? window.ink : window.muted
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    font.pixelSize: 21
                }
                background: Rectangle {
                    radius: 7
                    color: parent.hovered ? "#263952" : "transparent"
                }
            }
        }
        contentItem: ColumnLayout {
            spacing: 10
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Repeater {
                    model: [
                        {label: "员工", key: "employees"},
                        {label: "待审批", key: "pending_users"},
                        {label: "线索", key: "leads"},
                        {label: "待同步", key: "pending_sync"},
                        {label: "活跃设备", key: "active_devices"},
                        {label: "备份", key: "backups"}
                    ]
                    delegate: Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 58
                        radius: 9
                        color: index % 2 === 0 ? "#17273d" : "#142135"
                        border.color: window.line
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 8
                            spacing: 2
                            Text {
                                text: modelData.label
                                color: window.muted
                                font.pixelSize: 10
                                Layout.fillWidth: true
                            }
                            Text {
                                text: String((backend.adminDashboard.summary || {})[modelData.key] || 0)
                                color: window.ink
                                font.pixelSize: 18
                                font.weight: Font.DemiBold
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }
            Text {
                visible: String(backend.adminDashboard.error || "") !== ""
                text: "操作失败：" + String(backend.adminDashboard.error || "")
                color: "#ff9b9b"
                font.pixelSize: 11
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            Text {
                visible: String(((backend.adminDashboard.last_action || {}).message) || "") !== ""
                text: String((backend.adminDashboard.last_action || {}).message || "")
                color: window.green
                font.pixelSize: 11
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            TabBar {
                id: adminCenterTab
                Layout.fillWidth: true
                TabButton { text: "员工数据" }
                TabButton { text: "设备管理" }
                TabButton { text: "审计日志" }
                TabButton { text: "备份恢复" }
            }
            StackLayout {
                currentIndex: adminCenterTab.currentIndex
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                Item {
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 6
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "员工数据隔离概览"; color: window.ink; font.pixelSize: 13; font.weight: Font.Medium; Layout.fillWidth: true }
                            AppButton {
                                text: "刷新"
                                Layout.preferredWidth: 70
                                Layout.preferredHeight: 28
                                onClicked: backend.refreshAdminDashboard()
                                contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                background: Rectangle { radius: 6; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                            }
                            AppButton {
                                text: "用户审批"
                                Layout.preferredWidth: 82
                                Layout.preferredHeight: 28
                                onClicked: { backend.refreshAuthUsers(); adminUsersDialog.open() }
                                contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                background: Rectangle { radius: 6; color: "transparent"; border.color: parent.hovered ? window.blue : window.line }
                            }
                        }
                        ListView {
                            id: adminEmployeeList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 6
                            model: backend.adminDashboard.employees || []
                            delegate: Rectangle {
                                width: adminEmployeeList.width
                                height: 70
                                radius: 8
                                color: index % 2 === 0 ? "#17273d" : "#142135"
                                border.color: window.line
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: 10
                                    spacing: 12
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 3
                                        Text { text: (modelData.employee_name || "未填写姓名") + " · " + (modelData.username || ""); color: window.ink; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                                        Text { text: "状态：" + (modelData.status === "approved" ? "已通过" : (modelData.status === "pending" ? "待审批" : "已停用")) + " · 最近登录：" + (modelData.last_login_at || "暂无"); color: window.muted; font.pixelSize: 10; Layout.fillWidth: true; elide: Text.ElideRight }
                                    }
                                    Text { text: "任务 " + (modelData.tasks || 0) + "\n账号 " + (modelData.accounts || 0) + "\n线索 " + (modelData.leads || 0); color: window.blue; font.pixelSize: 10; Layout.preferredWidth: 90; horizontalAlignment: Text.AlignRight }
                                    Text { text: "互动 " + (modelData.interactions || 0) + "\n发布 " + (modelData.publish_drafts || 0) + "\n待同步 " + (modelData.pending_sync || 0); color: window.muted; font.pixelSize: 10; Layout.preferredWidth: 100; horizontalAlignment: Text.AlignRight }
                                }
                            }
                            Text { anchors.centerIn: parent; visible: adminEmployeeList.count === 0; text: "暂无员工数据"; color: window.muted; font.pixelSize: 12 }
                        }
                    }
                }
                Item {
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 6
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "员工设备"; color: window.ink; font.pixelSize: 13; font.weight: Font.Medium; Layout.fillWidth: true }
                            Text { text: "停用后该设备下次登录将被拒绝"; color: window.muted; font.pixelSize: 10 }
                        }
                        ListView {
                            id: adminDeviceList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 6
                            model: backend.adminDashboard.devices || []
                            delegate: Rectangle {
                                width: adminDeviceList.width
                                height: 62
                                radius: 8
                                color: index % 2 === 0 ? "#17273d" : "#142135"
                                border.color: window.line
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: 10
                                    spacing: 10
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 3
                                        Text { text: (modelData.device_name || "未命名设备") + " · " + (modelData.employee_name || modelData.username || ""); color: window.ink; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                                        Text { text: "最后活动：" + (modelData.last_seen_at || "暂无") + " · 版本：" + (modelData.client_version || "未知"); color: window.muted; font.pixelSize: 10; Layout.fillWidth: true; elide: Text.ElideRight }
                                    }
                                    Text { text: modelData.status === "active" ? "正常" : "已停用"; color: modelData.status === "active" ? window.green : window.amber; Layout.preferredWidth: 58; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 11 }
                                    AppButton {
                                        text: modelData.status === "active" ? "停用" : "启用"
                                        Layout.preferredWidth: 62
                                        Layout.preferredHeight: 28
                                        onClicked: backend.setAdminDeviceStatus(Number(modelData.id), modelData.status === "active" ? "disabled" : "active")
                                        contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                        background: Rectangle { radius: 6; color: parent.hovered ? window.panel2 : "transparent"; border.color: parent.hovered ? window.blue : window.line }
                                    }
                                }
                            }
                            Text { anchors.centerIn: parent; visible: adminDeviceList.count === 0; text: "暂无登记设备"; color: window.muted; font.pixelSize: 12 }
                        }
                    }
                }
                Item {
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 6
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "管理员审计日志"; color: window.ink; font.pixelSize: 13; font.weight: Font.Medium; Layout.fillWidth: true }
                            Text { text: "正常 " + (((backend.adminDashboard.audit || {}).counts || {}).normal || 0) + " · 警告 " + (((backend.adminDashboard.audit || {}).counts || {}).warning || 0) + " · 错误 " + (((backend.adminDashboard.audit || {}).counts || {}).error || 0); color: window.muted; font.pixelSize: 10 }
                            AppButton {
                                text: "刷新"
                                Layout.preferredWidth: 70
                                Layout.preferredHeight: 28
                                onClicked: backend.refreshAdminDashboard()
                                contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                background: Rectangle { radius: 6; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                            }
                        }
                        ListView {
                            id: adminAuditList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 2
                            model: (backend.adminDashboard.audit || {}).items || []
                            delegate: Rectangle {
                                width: adminAuditList.width
                                height: 50
                                radius: 6
                                color: index % 2 === 0 ? "#17273d" : "#142135"
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: 8
                                    spacing: 8
                                    Text { text: String(modelData.timestamp || "").slice(11, 19); color: window.muted; Layout.preferredWidth: 62; font.pixelSize: 10 }
                                    Text { text: modelData.level === "error" ? "错误" : (modelData.level === "warning" ? "警告" : "正常"); color: modelData.level === "error" ? "#ff9b9b" : (modelData.level === "warning" ? window.amber : window.green); Layout.preferredWidth: 40; font.pixelSize: 10 }
                                    Text { text: (modelData.actor_username || "系统") + " · " + (modelData.action || modelData.event || "操作"); color: window.blue; Layout.preferredWidth: 150; font.pixelSize: 10; elide: Text.ElideRight }
                                    Text { text: modelData.message || ""; color: window.ink; Layout.fillWidth: true; font.pixelSize: 10; elide: Text.ElideRight }
                                }
                            }
                            Text { anchors.centerIn: parent; visible: adminAuditList.count === 0; text: "暂无审计日志"; color: window.muted; font.pixelSize: 12 }
                        }
                    }
                }
                Item {
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 6
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "数据库备份与恢复"; color: window.ink; font.pixelSize: 13; font.weight: Font.Medium; Layout.fillWidth: true }
                            AppButton {
                                text: "立即备份"
                                Layout.preferredWidth: 88
                                Layout.preferredHeight: 30
                                onClicked: backend.createAdminBackup()
                                contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                background: Rectangle { radius: 7; color: window.blue; border.color: window.line }
                            }
                            AppButton {
                                text: "刷新"
                                Layout.preferredWidth: 70
                                Layout.preferredHeight: 30
                                onClicked: backend.refreshAdminBackups()
                                contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                                background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line }
                            }
                        }
                        Text { text: "恢复操作只生成校验后的新文件，不覆盖当前运行数据库；需要切换数据时再由管理员安排停机替换。"; color: window.amber; font.pixelSize: 10; Layout.fillWidth: true; elide: Text.ElideRight }
                        ListView {
                            id: adminBackupList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 6
                            model: backend.adminDashboard.backups || []
                            delegate: Rectangle {
                                width: adminBackupList.width
                                height: 70
                                radius: 8
                                color: index % 2 === 0 ? "#17273d" : "#142135"
                                border.color: window.line
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: 10
                                    spacing: 10
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 3
                                        Text { text: "备份#" + modelData.id + " · " + (modelData.kind || "手动") + " · " + (modelData.verified ? "已校验" : "未校验"); color: modelData.verified ? window.green : window.amber; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                                        Text { text: (modelData.created_at || "") + " · " + Math.round(Number(modelData.size_bytes || 0) / 1024) + " KB"; color: window.muted; font.pixelSize: 10; Layout.fillWidth: true; elide: Text.ElideRight }
                                    }
                                    AppButton {
                                        text: "校验"
                                        Layout.preferredWidth: 62
                                        Layout.preferredHeight: 28
                                        onClicked: backend.validateAdminBackup(Number(modelData.id))
                                        contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                        background: Rectangle { radius: 6; color: "transparent"; border.color: parent.hovered ? window.blue : window.line }
                                    }
                                    AppButton {
                                        text: "恢复副本"
                                        Layout.preferredWidth: 78
                                        Layout.preferredHeight: 28
                                        onClicked: backend.restoreAdminBackup(Number(modelData.id))
                                        contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                                        background: Rectangle { radius: 6; color: "transparent"; border.color: parent.hovered ? window.blue : window.line }
                                    }
                                }
                            }
                            Text { anchors.centerIn: parent; visible: adminBackupList.count === 0; text: "暂无备份记录"; color: window.muted; font.pixelSize: 12 }
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: deleteConfirmDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(520, window.width - 80)
        title: "确认删除任务"
        background: Rectangle { radius: 14; color: window.panel; border.color: "#8b4d5a" }
        header: Rectangle {
            implicitHeight: 44
            color: "#3a1f2b"
            Text { anchors.fill: parent; anchors.leftMargin: 16; text: deleteConfirmDialog.title; color: "#ffd9df"; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
        }
        contentItem: ColumnLayout {
            spacing: 10
            Text {
                text: "确定删除任务 #" + tasksPage.deleteCandidateId + " 吗？"
                color: window.ink
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            Text {
                text: tasksPage.deleteCandidateKeyword ? "关键词：" + tasksPage.deleteCandidateKeyword : ""
                color: window.muted
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            Text {
                text: "删除会清理该任务及其采集数据，操作不可恢复。运行中的任务会先安全停止。"
                color: window.amber
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }
        footer: RowLayout {
            spacing: 8
            Item { Layout.fillWidth: true }
            AppButton {
                text: "取消"
                onClicked: deleteConfirmDialog.reject()
                contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
            }
            AppButton {
                text: "确认删除"
                onClicked: tasksPage.confirmDelete()
                contentItem: Text { text: parent.text; color: "#fff1f3"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                background: Rectangle { radius: 7; color: "#b95768"; border.color: "#e97583" }
            }
        }
    }

    FolderDialog {
        id: outputFolderDialog
        title: "选择输出目录"
        onAccepted: {
            var path = String(selectedFolder || "")
            if (path.indexOf("file:///") === 0)
                path = decodeURIComponent(path.slice(8))
            taskOutputField.text = path.replace(/\//g, "\\")
        }
    }

    Dialog {
        id: realSendConfirmDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(520, window.width - 80)
        title: "确认开启真实发送"
        background: Rectangle { radius: 14; color: window.panel; border.color: "#8b4d5a" }
        header: Rectangle { implicitHeight: 44; color: "#3a1f2b"; Text { anchors.fill: parent; anchors.leftMargin: 16; text: realSendConfirmDialog.title; color: "#ffd9df"; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold } }
        contentItem: ColumnLayout {
            spacing: 10
            Text { text: "开启后，点击“发送”会执行平台页面上的最终发送按钮。请确认当前账号、内容和目标均已人工审核。"; color: window.ink; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 12 }
            Text { text: "测试阶段建议保持关闭；关闭时只填入回复框，不会提交。"; color: window.amber; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
        }
        footer: RowLayout {
            spacing: 8
            Item { Layout.fillWidth: true }
            AppButton { text: "取消"; onClicked: realSendConfirmDialog.reject(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
            AppButton { text: "确认开启"; onClicked: realSendConfirmDialog.accept(); contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "#e97583" } }
        }
        onAccepted: { interactionPage2.realSendEnabled = true; interactionPage2.realSendConfirmed = true; realSendSwitch.checked = true }
        onRejected: { interactionPage2.realSendEnabled = false; interactionPage2.realSendConfirmed = false; realSendSwitch.checked = false }
    }

    Dialog {
        id: realPublishEnableDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(540, window.width - 80)
        title: "开启真实发布"
        background: Rectangle { radius: 14; color: window.panel; border.color: "#8b4d5a" }
        header: Rectangle { implicitHeight: 44; color: "#3a1f2b"; Text { anchors.fill: parent; anchors.leftMargin: 16; text: realPublishEnableDialog.title; color: "#ffd9df"; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold } }
        contentItem: ColumnLayout {
            spacing: 10
            Text { text: "开启后，发布中心的“开始真实发布”会打开当前平台账号，并点击页面上的最终发布按钮。"; color: window.ink; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 12 }
            Text { text: "请先确认平台、账号、标题、正文和素材均已人工审核。测试时请保持关闭，预览填充不会发送。"; color: window.amber; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
        }
        footer: RowLayout {
            spacing: 8
            Item { Layout.fillWidth: true }
            AppButton { text: "取消"; onClicked: realPublishEnableDialog.reject(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
            AppButton { text: "确认开启"; onClicked: realPublishEnableDialog.accept(); contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "#e97583" } }
        }
        onAccepted: publishPage.realPublishEnabled = true
    }

    Dialog {
        id: realPublishConfirmDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(560, window.width - 80)
        title: "确认开始真实发布"
        background: Rectangle { radius: 14; color: window.panel; border.color: "#8b4d5a" }
        header: Rectangle { implicitHeight: 44; color: "#3a1f2b"; Text { anchors.fill: parent; anchors.leftMargin: 16; text: realPublishConfirmDialog.title; color: "#ffd9df"; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold } }
        contentItem: ColumnLayout {
            spacing: 10
            Text { text: "即将打开“" + publishPage.selectedAccountName() + "”并在“" + publishPage.platformLabel(publishPage.selectedPlatform) + "”页面执行最终发布。"; color: window.ink; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 12 }
            Text { text: "此操作会产生真实平台状态变化。只有已批准或待发布内容可以执行。"; color: "#ffb0b0"; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
        }
        footer: RowLayout {
            spacing: 8
            Item { Layout.fillWidth: true }
            AppButton { text: "取消"; onClicked: realPublishConfirmDialog.reject(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
            AppButton { text: "确认发布"; onClicked: realPublishConfirmDialog.accept(); contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "#e97583" } }
        }
        onAccepted: backend.realPublishDraft(
            publishPage.selectedDraftId, publishPage.selectedPlatform,
            Number(publishPage.selectedAccountId),
            publishPage.editorTitle, publishPage.editorBody, publishPage.editorTopics)
    }

    Dialog {
        id: schedulePickerDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(540, window.width - 64)
        height: Math.min(580, window.height - 70)
        title: "选择发布时间"
        property string draftDate: ""
        property string draftHour: ""
        property string draftMinute: ""
        property int calendarYear: 0
        property int calendarMonth: 0
        property var calendarCells: []

        function monthValue(year, month) {
            return Number(year || 0) * 12 + Number(month || 0)
        }
        function monthTitle() {
            return calendarYear + "年" + publishPage.padSchedulePart(calendarMonth) + "月"
        }
        function minDateValue() {
            var values = publishPage.scheduleDateOptions()
            return values.length ? String(values[0].value || "") : ""
        }
        function maxDateValue() {
            var values = publishPage.scheduleDateOptions()
            return values.length ? String(values[values.length - 1].value || "") : ""
        }
        function dateIsSelectable(value) {
            var text = String(value || "")
            return Boolean(text) && text >= minDateValue() && text <= maxDateValue()
                && publishPage.scheduleHourOptionsFor(text).length > 0
        }
        function refreshCalendar() {
            if (!calendarYear || !calendarMonth) return
            var first = new Date(Date.UTC(calendarYear, calendarMonth - 1, 1))
            var mondayOffset = (first.getUTCDay() + 6) % 7
            var start = new Date(Date.UTC(calendarYear, calendarMonth - 1, 1 - mondayOffset))
            var cells = []
            for (var i = 0; i < 42; i++) {
                var current = new Date(start.getTime() + i * 24 * 60 * 60 * 1000)
                var year = current.getUTCFullYear()
                var month = current.getUTCMonth() + 1
                var day = current.getUTCDate()
                var value = year + "-" + publishPage.padSchedulePart(month) + "-" + publishPage.padSchedulePart(day)
                var inMonth = year === calendarYear && month === calendarMonth
                cells.push({
                    value: value,
                    day: day,
                    inMonth: inMonth,
                    enabled: inMonth && dateIsSelectable(value),
                    selected: value === draftDate,
                    today: value === publishPage.scheduleTodayValue()
                })
            }
            calendarCells = cells
        }
        function syncTimeControls() {
            var hours = hourOptions()
            var minutes = minuteOptions()
            var hourIndex = publishPage.scheduleOptionIndex(hours, draftHour)
            var minuteIndex = publishPage.scheduleOptionIndex(minutes, draftMinute)
            if (schedulePickerHourChooser.count > 0 && hourIndex >= 0
                    && schedulePickerHourChooser.currentIndex !== hourIndex)
                schedulePickerHourChooser.currentIndex = hourIndex
            if (schedulePickerMinuteChooser.count > 0 && minuteIndex >= 0
                    && schedulePickerMinuteChooser.currentIndex !== minuteIndex)
                schedulePickerMinuteChooser.currentIndex = minuteIndex
        }
        function ensureDraftTime() {
            var hours = hourOptions()
            if (publishPage.scheduleOptionIndex(hours, draftHour) < 0)
                draftHour = hours.length ? String(hours[0].value) : ""
            var minutes = minuteOptions()
            if (publishPage.scheduleOptionIndex(minutes, draftMinute) < 0)
                draftMinute = minutes.length ? String(minutes[0].value) : ""
            Qt.callLater(syncTimeControls)
        }
        function hourOptions() {
            return publishPage.scheduleHourOptionsFor(draftDate)
        }
        function minuteOptions() {
            return publishPage.scheduleMinuteOptionsFor(draftDate, draftHour)
        }
        function openFor() {
            publishPage.ensureSchedulePickerSelection()
            draftDate = String(publishPage.scheduleDate || "")
            draftHour = String(publishPage.scheduleHour || "")
            draftMinute = String(publishPage.scheduleMinute || "")
            var parts = publishPage.scheduleDateParts(draftDate)
            if (!parts) {
                var now = publishPage.beijingClockParts()
                parts = {year: now.year, month: now.month, day: now.day}
                draftDate = publishPage.scheduleTodayValue()
                ensureDraftTime()
            }
            calendarYear = parts.year
            calendarMonth = parts.month
            ensureDraftTime()
            refreshCalendar()
            open()
        }
        function moveMonth(offset) {
            var next = new Date(Date.UTC(calendarYear, calendarMonth - 1 + Number(offset || 0), 1))
            var nextYear = next.getUTCFullYear()
            var nextMonth = next.getUTCMonth() + 1
            var minParts = publishPage.scheduleDateParts(minDateValue())
            var maxParts = publishPage.scheduleDateParts(maxDateValue())
            if (!minParts || !maxParts) return
            var target = monthValue(nextYear, nextMonth)
            if (target < monthValue(minParts.year, minParts.month)
                    || target > monthValue(maxParts.year, maxParts.month)) return
            calendarYear = nextYear
            calendarMonth = nextMonth
            refreshCalendar()
        }
        function selectDate(value) {
            if (!dateIsSelectable(value)) return
            draftDate = String(value)
            ensureDraftTime()
            refreshCalendar()
        }
        function confirmSelection() {
            if (!draftDate || !draftHour || !draftMinute) return false
            publishPage.scheduleDate = draftDate
            publishPage.scheduleHour = draftHour
            publishPage.scheduleMinute = draftMinute
            publishPage.scheduledAt = publishPage.buildScheduledAt()
            publishPage.scheduleNotice = ""
            return true
        }

        Overlay.modal: Rectangle { color: "#99060b14" }
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 48
            color: window.panel2
            border.color: window.line
            Text {
                anchors.fill: parent
                anchors.leftMargin: 18
                anchors.rightMargin: 52
                text: schedulePickerDialog.title
                color: window.ink
                verticalAlignment: Text.AlignVCenter
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }
            AppButton {
                anchors.right: parent.right
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                width: 32; height: 32; text: "×"
                onClicked: schedulePickerDialog.reject()
                contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 }
                background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" }
            }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: "北京时间 (UTC+8)"; color: window.muted; font.pixelSize: 10; Layout.fillWidth: true }
            RowLayout {
                Layout.fillWidth: true
                AppButton {
                    text: "‹"; Layout.preferredWidth: 38; Layout.preferredHeight: 34
                    enabled: schedulePickerDialog.calendarCells.length > 0
                    onClicked: schedulePickerDialog.moveMonth(-1)
                    contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 22 }
                    background: Rectangle { radius: 7; color: parent.hovered ? "#1e3555" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                }
                Text { text: schedulePickerDialog.monthTitle(); color: window.ink; font.pixelSize: 17; font.weight: Font.DemiBold; horizontalAlignment: Text.AlignHCenter; Layout.fillWidth: true }
                AppButton {
                    text: "›"; Layout.preferredWidth: 38; Layout.preferredHeight: 34
                    enabled: schedulePickerDialog.calendarCells.length > 0
                    onClicked: schedulePickerDialog.moveMonth(1)
                    contentItem: Text { text: parent.text; color: parent.enabled ? window.ink : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 22 }
                    background: Rectangle { radius: 7; color: parent.hovered ? "#1e3555" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                }
            }
            GridLayout {
                Layout.fillWidth: true
                columns: 7
                columnSpacing: 4
                rowSpacing: 4
                Repeater {
                    model: ["一", "二", "三", "四", "五", "六", "日"]
                    delegate: Text {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        text: modelData
                        color: window.muted
                        font.pixelSize: 10
                    }
                }
                Repeater {
                    model: schedulePickerDialog.calendarCells
                    delegate: AppButton {
                        property var cellData: modelData
                        Layout.fillWidth: true
                        Layout.preferredHeight: 36
                        enabled: Boolean(cellData.enabled)
                        text: String(cellData.day || "")
                        onClicked: schedulePickerDialog.selectDate(cellData.value)
                        contentItem: Text {
                            text: parent.text
                            color: !cellData.inMonth ? "#536681" : (cellData.enabled ? window.ink : "#536681")
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            font.pixelSize: 11
                            font.weight: cellData.selected ? Font.DemiBold : Font.Normal
                        }
                        background: Rectangle {
                            radius: 7
                            color: cellData.selected ? window.blue : (parent.hovered && parent.enabled ? "#1e3555" : "transparent")
                            border.color: cellData.today && !cellData.selected ? window.blue : (parent.hovered && parent.enabled ? window.blue : "transparent")
                            border.width: cellData.today || (parent.hovered && parent.enabled) ? 1 : 0
                        }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: window.line }
            RowLayout {
                Layout.fillWidth: true
                ColumnLayout {
                    Layout.fillWidth: true; spacing: 4
                    Text { text: "小时"; color: window.muted; font.pixelSize: 10 }
                    ComboBox {
                        id: schedulePickerHourChooser
                        Layout.fillWidth: true; Layout.preferredHeight: 34
                        model: schedulePickerDialog.hourOptions()
                        textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                        onModelChanged: schedulePickerDialog.syncTimeControls()
                        onActivated: {
                            schedulePickerDialog.draftHour = String(currentValue || "")
                            schedulePickerDialog.ensureDraftTime()
                        }
                        contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 }
                        background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true; spacing: 4
                    Text { text: "分钟"; color: window.muted; font.pixelSize: 10 }
                    ComboBox {
                        id: schedulePickerMinuteChooser
                        Layout.fillWidth: true; Layout.preferredHeight: 34
                        model: schedulePickerDialog.minuteOptions()
                        textRole: "label"; valueRole: "value"; delegate: darkComboDelegate
                        onModelChanged: schedulePickerDialog.syncTimeControls()
                        onActivated: {
                            schedulePickerDialog.draftMinute = String(currentValue || "")
                            schedulePickerDialog.ensureDraftTime()
                        }
                        contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 }
                        background: Rectangle { radius: 7; color: window.panel2; border.color: parent.activeFocus ? window.blue : window.line }
                    }
                }
            }
            Text { text: "不可选择过去的时间 · 当前北京时间 " + String(backend.beijingNowText || "").slice(0, 16); color: window.muted; font.pixelSize: 10; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        }
        footer: RowLayout {
            spacing: 8
            Item { Layout.fillWidth: true }
            AppButton {
                text: "取消"; onClicked: schedulePickerDialog.reject()
                contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                background: Rectangle { radius: 7; color: parent.hovered ? window.panel2 : "transparent"; border.color: window.line }
            }
            AppButton {
                text: "确定"; enabled: Boolean(schedulePickerDialog.draftDate && schedulePickerDialog.draftHour && schedulePickerDialog.draftMinute)
                onClicked: if (schedulePickerDialog.confirmSelection()) schedulePickerDialog.accept()
                contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11; font.weight: Font.DemiBold }
                background: Rectangle { radius: 7; color: parent.enabled ? (parent.hovered ? "#8bbaff" : window.blue) : window.panel2 }
            }
        }
    }

    Dialog {
        id: monitorDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(560, window.width - 80)
        height: 300
        property var taskData: ({})
        function openFor(item) {
            taskData = item || ({})
            var rule = taskData.monitoring_rule || ({})
            monitorEnabledCheck.checked = Boolean(rule.enabled)
            var seconds = Number(rule.interval_seconds || 3600)
            monitorHoursField.text = String(Math.floor(seconds / 3600))
            monitorMinutesField.text = String(Math.floor((seconds % 3600) / 60))
            monitorSortChooser.currentIndex = 0
            monitorDialog.open()
        }
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 44; color: window.panel2; border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 48; text: "定时增量监控设置"; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
            AppButton { anchors.right: parent.right; anchors.rightMargin: 7; anchors.verticalCenter: parent.verticalCenter; width: 32; height: 32; text: "×"; onClicked: monitorDialog.close(); contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 } background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" } }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: "任务 #" + (monitorDialog.taskData.id || "") + " · " + (monitorDialog.taskData.keyword || ""); color: window.ink; font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "监控状态"; color: window.muted; Layout.preferredWidth: 100; font.pixelSize: 11 }
                AppCheckBox { id: monitorEnabledCheck; text: "启用定时增量监控"; Layout.fillWidth: true }
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "执行间隔"; color: window.muted; Layout.preferredWidth: 100; font.pixelSize: 11 }
                TextField { id: monitorHoursField; Layout.preferredWidth: 90; color: window.ink; inputMethodHints: Qt.ImhDigitsOnly; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                Text { text: "小时"; color: window.muted; font.pixelSize: 11 }
                TextField { id: monitorMinutesField; Layout.preferredWidth: 90; color: window.ink; inputMethodHints: Qt.ImhDigitsOnly; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                Text { text: "分钟"; color: window.muted; font.pixelSize: 11 }
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "默认排序"; color: window.muted; Layout.preferredWidth: 100; font.pixelSize: 11 }
                ComboBox { id: monitorSortChooser; Layout.fillWidth: true; model: [{label: "最新发布", value: "latest"}, {label: "综合排序", value: "default"}]; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate; contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            Item { Layout.fillHeight: true }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                AppButton { text: "取消"; onClicked: monitorDialog.close(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
                AppButton { text: "保存设置"; onClicked: { var hours = Math.max(0, parseInt(monitorHoursField.text || "0")); var minutes = Math.max(0, parseInt(monitorMinutesField.text || "0")); backend.configureMonitoring(Number(monitorDialog.taskData.id || 0), Math.max(60, hours * 3600 + minutes * 60), monitorEnabledCheck.checked, String(monitorSortChooser.currentValue || "latest")); monitorDialog.close() } contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: window.blue } }
            }
        }
    }

    Dialog {
        id: leadTagDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(480, window.width - 80)
        height: 210
        title: "批量打标签"
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 44; color: window.panel2; border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 48; text: leadTagDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
            AppButton { anchors.right: parent.right; anchors.rightMargin: 7; anchors.verticalCenter: parent.verticalCenter; width: 32; height: 32; text: "×"; onClicked: leadTagDialog.close(); contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 } background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" } }
        }
        contentItem: ColumnLayout {
            spacing: 10
            Text { text: "已选择 " + leadPage.selectedLeadIds.length + " 条线索"; color: window.muted; font.pixelSize: 11 }
            RowLayout { Layout.fillWidth: true; Text { text: "标签内容"; color: window.muted; Layout.preferredWidth: 80; font.pixelSize: 11 } TextField { id: leadTagField; Layout.fillWidth: true; color: window.ink; palette.placeholderText: window.muted; placeholderText: "例如：重点跟进、待补充资料"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } } }
            RowLayout { Layout.fillWidth: true; Item { Layout.fillWidth: true } AppButton { text: "取消"; onClicked: leadTagDialog.close(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } } AppButton { text: "保存标签"; enabled: leadTagField.text.trim().length > 0; onClicked: { backend.tagLeads(leadPage.selectedLeadIds, leadTagField.text); leadPage.selectedLeadIds = []; leadTagDialog.close() } contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: parent.enabled ? window.blue : window.panel2 } } }
        }
        onOpened: { leadTagField.text = ""; leadTagField.forceActiveFocus() }
    }

    Dialog {
        id: taskDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(720, window.width - 80)
        height: Math.min(760, window.height - 50)
        title: "新建采集任务"
        property var selectedAccountNames: []
        property var selectedCollectTypes: ["video_info", "author_info", "engagement", "comments", "comment_user", "region", "intent"]
        property var keywordGroupModel: [{id: 0, label: "不使用关键词组", name: ""}]
        Connections {
            target: backend
            function onViewChanged() {
                if (taskDialog.visible) taskDialog.rebuildKeywordGroups()
            }
        }

        property var collectTypeOptions: [
            {key: "video_info", label: "作品基础信息"},
            {key: "author_info", label: "作者/账号信息"},
            {key: "engagement", label: "点赞、收藏、分享"},
            {key: "comments", label: "评论内容"},
            {key: "comment_user", label: "评论用户信息"},
            {key: "region", label: "地区/省份"},
            {key: "intent", label: "意向识别"}
        ]

        function sortOptionsFor(platform) {
            if (platform === "xhs") return [{label: "综合排序", value: "default"}, {label: "最新", value: "latest"}, {label: "最热", value: "hot"}]
            if (platform === "weibo") return [{label: "综合", value: "default"}, {label: "实时", value: "realtime"}, {label: "热门", value: "hot"}]
            if (platform === "bilibili") return [{label: "综合排序", value: "totalrank"}, {label: "最多播放", value: "click"}, {label: "最新发布", value: "pubdate"}, {label: "最多弹幕", value: "dm"}, {label: "最多收藏", value: "stow"}]
            if (platform === "kuaishou") return [{label: "综合排序", value: "default"}]
            if (platform === "tieba") return [{label: "最新帖子", value: "latest"}, {label: "热门帖子", value: "hot"}]
            return [{label: "综合排序", value: "default"}, {label: "最多点赞", value: "most_like"}, {label: "最新发布", value: "latest"}]
        }

        function accountOptionsFor(platform) {
            var result = []
            for (var i = 0; i < backend.accountRows.length; i++) {
                var item = backend.accountRows[i]
                if (item.platform === platform) result.push(item)
            }
            return result
        }

        function rebuildKeywordGroups() {
            var result = [{id: 0, label: "不使用关键词组"}]
            var platform = String(taskPlatformChooser.currentValue || "douyin")
            for (var i = 0; i < backend.keywordGroups.length; i++) {
                var group = backend.keywordGroups[i]
                if (!group.platform || group.platform === platform)
                    result.push({id: group.id, label: group.name, name: group.name})
            }
            keywordGroupModel = result
        }

        function syncAccountText() { taskAccountsField.text = selectedAccountNames.join(", ") }
        function toggleAccount(name, checked) {
            var next = selectedAccountNames.slice()
            var pos = next.indexOf(name)
            if (checked && pos < 0) next.push(name)
            if (!checked && pos >= 0) next.splice(pos, 1)
            selectedAccountNames = next
            syncAccountText()
        }
        function toggleCollectType(key, checked) {
            var next = selectedCollectTypes.slice()
            var pos = next.indexOf(key)
            if (checked && pos < 0) next.push(key)
            if (!checked && pos >= 0) next.splice(pos, 1)
            selectedCollectTypes = next
        }
        function resetForPlatform() {
            selectedAccountNames = []
            syncAccountText()
            if (taskGroupChooser) taskGroupChooser.currentIndex = 0
            if (taskSortChooser) taskSortChooser.currentIndex = 0
            rebuildKeywordGroups()
        }
        onOpened: {
            selectedAccountNames = []
            selectedCollectTypes = ["video_info", "author_info", "engagement", "comments", "comment_user", "region", "intent"]
            taskKeywordField.text = ""
            taskAccountsField.text = ""
            taskTargetField.text = "100"
            taskCollectModeChooser.currentIndex = 1
            taskBatchField.text = "10"
            taskCooldownField.text = "60"
            taskMonitorField.text = "3600"
            taskOutputField.text = "data/exports"
            taskPlatformChooser.currentIndex = 0
            taskModeChooser.currentIndex = 0
            taskGroupChooser.currentIndex = 0
            taskSortChooser.currentIndex = 0
            rebuildKeywordGroups()
            backend.refreshKeywordGroups()
        }
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 42
            color: window.panel2
            border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 48; text: taskDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
            AppButton {
                anchors.right: parent.right
                anchors.rightMargin: 7
                anchors.verticalCenter: parent.verticalCenter
                width: 32
                height: 32
                text: "×"
                onClicked: taskDialog.close()
                contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 }
                background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" }
            }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: "完整复用 1.2：关键词组按词逐个搜索；排序、采集内容和账号均按平台分别设置。"; color: window.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
            RowLayout { Layout.fillWidth: true; Text { text: "平台"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                ComboBox { id: taskPlatformChooser; Layout.fillWidth: true; model: [{label: "抖音", value: "douyin"}, {label: "小红书", value: "xhs"}, {label: "B站", value: "bilibili"}, {label: "微博", value: "weibo"}, {label: "快手", value: "kuaishou"}]; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate; onActivated: taskDialog.resetForPlatform(); contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "搜索关键词"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                TextField { id: taskKeywordField; Layout.fillWidth: true; color: window.ink; palette.placeholderText: window.muted; placeholderText: "例如：郑州早教；选择关键词组后自动填入组名"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "关键词组"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                ComboBox { id: taskGroupChooser; Layout.fillWidth: true; model: taskDialog.keywordGroupModel; textRole: "label"; valueRole: "id"; delegate: darkComboDelegate; onActivated: { if (currentValue > 0) taskKeywordField.text = currentText } contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                AppButton { Layout.preferredWidth: 116; text: "＋ 新建关键词组"; onClicked: keywordGroupDialog.openForCreate(); contentItem: Text { text: parent.text; color: parent.enabled ? window.blue : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "搜索排序"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                ComboBox { id: taskSortChooser; Layout.fillWidth: true; model: taskDialog.sortOptionsFor(String(taskPlatformChooser.currentValue || "douyin")); textRole: "label"; valueRole: "value"; delegate: darkComboDelegate; contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "采集模式"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                ComboBox { id: taskCollectModeChooser; Layout.fillWidth: true; model: [{label: "快速（最多100条）", value: "fast"}, {label: "标准（常规采集）", value: "standard"}, {label: "深度（最多10000条）", value: "deep"}]; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate; contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "执行方式"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                ComboBox { id: taskModeChooser; Layout.fillWidth: true; model: [{label: "单次采集", value: "once"}, {label: "定时增量监控", value: "monitoring"}]; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate; contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; spacing: 10
                Text { text: "每关键词目标"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                TextField { id: taskTargetField; Layout.preferredWidth: 125; text: "100"; color: window.ink; palette.placeholderText: window.muted; inputMethodHints: Qt.ImhDigitsOnly; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                Text { text: "每批数量"; color: window.muted; font.pixelSize: 11 }
                TextField { id: taskBatchField; Layout.preferredWidth: 90; text: "10"; color: window.ink; palette.placeholderText: window.muted; inputMethodHints: Qt.ImhDigitsOnly; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                Text { text: "冷却秒数"; color: window.muted; font.pixelSize: 11 }
                TextField { id: taskCooldownField; Layout.preferredWidth: 90; text: "60"; color: window.ink; palette.placeholderText: window.muted; inputMethodHints: Qt.ImhDigitsOnly; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "监控间隔（秒）"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                TextField { id: taskMonitorField; Layout.preferredWidth: 125; text: "3600"; color: window.ink; palette.placeholderText: window.muted; inputMethodHints: Qt.ImhDigitsOnly; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                AppCheckBox { id: taskOnlyComments; text: "仅保留有评论"; checked: false }
                Item { Layout.fillWidth: true }
            }
            Text { text: "采集内容"; color: window.muted; font.pixelSize: 11 }
            GridLayout {
                Layout.fillWidth: true
                columns: 3
                columnSpacing: 14
                rowSpacing: 2
                Repeater {
                    model: taskDialog.collectTypeOptions
                    delegate: AppCheckBox {
                        Layout.fillWidth: true
                        property string collectKey: modelData.key
                        text: modelData.label
                        checked: taskDialog.selectedCollectTypes.indexOf(collectKey) >= 0
                        onToggled: taskDialog.toggleCollectType(collectKey, checked)
                    }
                }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "参与账号"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                TextField { id: taskAccountsField; Layout.fillWidth: true; readOnly: true; color: window.ink; palette.placeholderText: window.muted; placeholderText: "请在下方勾选当前平台已绑定账号"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: 14
                rowSpacing: 2
                Repeater {
                    model: taskDialog.accountOptionsFor(String(taskPlatformChooser.currentValue || "douyin"))
                    delegate: AppCheckBox {
                        Layout.fillWidth: true
                        property string accountName: String(modelData.account_key || modelData.name || "")
                        property string accountLabel: String(modelData.nickname || modelData.name || "未读取昵称")
                        text: accountLabel + " · " + (modelData.binding_label || "已绑定")
                        enabled: Boolean(modelData.window_id)
                        checked: taskDialog.selectedAccountNames.indexOf(accountName) >= 0
                        onToggled: taskDialog.toggleAccount(accountName, checked)
                    }
                }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "输出目录"; color: window.muted; Layout.preferredWidth: 104; font.pixelSize: 11 }
                TextField { id: taskOutputField; Layout.fillWidth: true; text: "data/exports"; color: window.ink; palette.placeholderText: window.muted; placeholderText: "留空使用默认目录"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                AppButton { Layout.preferredWidth: 86; text: "选择目录"; onClicked: outputFolderDialog.open(); contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 7; color: "transparent"; border.color: "#365174" } }
            }
            Item { Layout.fillHeight: true }
            RowLayout { Layout.fillWidth: true; Item { Layout.fillWidth: true }
                AppButton { text: "取消"; onClicked: taskDialog.close(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
                AppButton { text: "创建任务"; enabled: taskKeywordField.text.trim().length > 0 && taskDialog.selectedAccountNames.length > 0 && taskDialog.selectedCollectTypes.length > 0; onClicked: { backend.createTask(String(taskPlatformChooser.currentValue || "douyin"), taskKeywordField.text, parseInt(taskTargetField.text || "100"), taskAccountsField.text, String(taskModeChooser.currentValue || "once"), String(taskSortChooser.currentValue || "default"), parseInt(taskGroupChooser.currentValue || "0"), parseInt(taskBatchField.text || "10"), parseInt(taskCooldownField.text || "60"), taskOnlyComments.checked, taskDialog.selectedCollectTypes.join(","), taskOutputField.text, parseInt(taskMonitorField.text || "3600"), String(taskCollectModeChooser.currentValue || "standard")); taskDialog.close() } contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: parent.enabled ? window.blue : window.panel2 } }
            }
        }
    }

    Dialog {
        id: publishDraftDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(720, window.width - 80)
        height: Math.min(560, window.height - 60)
        title: "新建发布内容"
        property bool createDouyin: true
        property bool createXhs: true
        property bool createBilibili: true
        property bool createWeibo: true
        onOpened: {
            publishTitleField.text = ""
            publishBodyField.text = ""
            createDouyin = true
            createXhs = true
            createBilibili = true
            createWeibo = true
            publishTitleField.forceActiveFocus()
        }
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 42
            color: window.panel2
            border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 48; text: publishDraftDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
            AppButton {
                anchors.right: parent.right; anchors.rightMargin: 7; anchors.verticalCenter: parent.verticalCenter
                width: 32; height: 32; text: "×"
                onClicked: publishDraftDialog.close()
                contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 }
                background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" }
            }
        }
        contentItem: ColumnLayout {
            spacing: 11
            Text { text: "先创建一份统一内容，之后可以在发布中心分别编辑四个平台版本。"; color: window.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
            RowLayout { Layout.fillWidth: true
                Text { text: "内容标题"; color: window.muted; Layout.preferredWidth: 78; font.pixelSize: 11 }
                TextField { id: publishTitleField; Layout.fillWidth: true; color: window.ink; placeholderText: "可填写统一标题，后续可按平台修改"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            Text { text: "原始正文"; color: window.muted; font.pixelSize: 11 }
            TextArea { id: publishBodyField; Layout.fillWidth: true; Layout.fillHeight: true; color: window.ink; wrapMode: TextArea.Wrap; selectByMouse: true; placeholderText: "请输入完整内容，不填入任何平台页面"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            Text { text: "生成平台版本"; color: window.muted; font.pixelSize: 11 }
            RowLayout { Layout.fillWidth: true; spacing: 12
                AppCheckBox { text: "抖音"; checked: publishDraftDialog.createDouyin; onToggled: publishDraftDialog.createDouyin = checked }
                AppCheckBox { text: "小红书"; checked: publishDraftDialog.createXhs; onToggled: publishDraftDialog.createXhs = checked }
                AppCheckBox { text: "B站"; checked: publishDraftDialog.createBilibili; onToggled: publishDraftDialog.createBilibili = checked }
                AppCheckBox { text: "微博"; checked: publishDraftDialog.createWeibo; onToggled: publishDraftDialog.createWeibo = checked }
            }
            RowLayout { Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                AppButton {
                    text: "取消"; onClicked: publishDraftDialog.close()
                    contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
                }
                AppButton {
                    text: "创建草稿"
                    enabled: publishTitleField.text.trim().length > 0 || publishBodyField.text.trim().length > 0
                    onClicked: {
                        var values = []
                        if (publishDraftDialog.createDouyin) values.push("douyin")
                        if (publishDraftDialog.createXhs) values.push("xhs")
                        if (publishDraftDialog.createBilibili) values.push("bilibili")
                        if (publishDraftDialog.createWeibo) values.push("weibo")
                        if (values.length === 0) return
                        backend.createPublishDraft(publishTitleField.text, publishBodyField.text, "text", values.join(","))
                        publishDraftDialog.close()
                    }
                    contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { radius: 7; color: parent.enabled ? window.blue : window.panel2 }
                }
            }
        }
    }

    FileDialog {
        id: publishAssetDialog
        title: "选择发布素材"
        fileMode: FileDialog.OpenFiles
        nameFilters: ["图片和视频 (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff *.avif *.heic *.mp4 *.mov *.mkv *.avi *.webm *.flv *.wmv *.m4v *.mpeg *.mpg)", "所有文件 (*)"]
        onAccepted: {
            if (publishPage.selectedDraftId > 0)
                backend.addPublishAssets(publishPage.selectedDraftId, selectedFiles)
        }
    }

    Dialog {
        id: keywordGroupDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(660, window.width - 80)
        height: Math.min(590, window.height - 50)
        title: manageMode ? "编辑关键词/词组" : "新建关键词组"
        property bool manageMode: false
        property int editingGroupId: 0
        function resetFields() {
            keywordGroupNameField.text = ""
            keywordGroupCoreField.text = ""
            keywordGroupSynonymField.text = ""
            keywordGroupRegionField.text = ""
            keywordGroupExcludeField.text = ""
        }
        function openForCreate() {
            manageMode = false
            editingGroupId = 0
            open()
        }
        function openForManage() {
            manageMode = true
            editingGroupId = 0
            open()
        }
        function startNewGroup() {
            manageMode = true
            editingGroupId = 0
            keywordGroupEditChooser.currentIndex = -1
            resetFields()
            keywordGroupNameField.forceActiveFocus()
        }
        function loadManageGroup(groupId) {
            var id = Number(groupId || 0)
            var groups = backend.keywordGroups || []
            var group = null
            for (var i = 0; i < groups.length; i++) {
                if (Number(groups[i].id || 0) === id) { group = groups[i]; break }
            }
            if (!group) return
            editingGroupId = id
            keywordGroupNameField.text = String(group.name || "")
            var platform = String(group.platform || "")
            var platformIndex = 0
            for (var p = 0; p < keywordGroupPlatformChooser.model.length; p++) {
                if (String(keywordGroupPlatformChooser.model[p].value || "") === platform) { platformIndex = p; break }
            }
            keywordGroupPlatformChooser.currentIndex = platformIndex
            var terms = group.terms || {}
            keywordGroupCoreField.text = (terms.core || []).join("\n")
            keywordGroupSynonymField.text = (terms.synonym || []).join("\n")
            keywordGroupRegionField.text = (terms.region || []).join("\n")
            keywordGroupExcludeField.text = (terms.exclude || []).join("\n")
        }
        function syncManageGroups() {
            if (!manageMode) return
            var groups = backend.keywordGroups || []
            if (!groups.length) {
                editingGroupId = 0
                keywordGroupEditChooser.currentIndex = -1
                resetFields()
                return
            }
            var target = editingGroupId
            var found = false
            for (var i = 0; i < groups.length; i++) {
                if (Number(groups[i].id || 0) === Number(target || 0)) { found = true; break }
            }
            if (!found) target = Number(groups[0].id || 0)
            for (var j = 0; j < groups.length; j++) {
                if (Number(groups[j].id || 0) === target) {
                    keywordGroupEditChooser.currentIndex = j
                    loadManageGroup(target)
                    break
                }
            }
        }
        Connections {
            target: backend
            function onKeywordGroupsChanged() {
                if (keywordGroupDialog.visible) keywordGroupDialog.syncManageGroups()
            }
        }
        onOpened: {
            if (manageMode) {
                resetFields()
                backend.refreshKeywordGroups()
            } else {
                keywordGroupPlatformChooser.currentIndex = taskPlatformChooser.currentIndex
                resetFields()
                keywordGroupNameField.forceActiveFocus()
            }
        }
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 42
            color: window.panel2
            border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 48; text: keywordGroupDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
            AppButton {
                anchors.right: parent.right
                anchors.rightMargin: 7
                anchors.verticalCenter: parent.verticalCenter
                width: 32
                height: 32
                text: "×"
                onClicked: keywordGroupDialog.close()
                contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 }
                background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" }
            }
        }
        contentItem: ColumnLayout {
            spacing: 10
            Text { text: "保存后可在新建任务中按当前平台选择，关键词会按词逐个搜索。"; color: window.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
            RowLayout {
                visible: keywordGroupDialog.manageMode
                Layout.fillWidth: true
                Text { text: "编辑词组"; color: window.muted; Layout.preferredWidth: 92; font.pixelSize: 11 }
                ComboBox {
                    id: keywordGroupEditChooser
                    Layout.fillWidth: true
                    model: backend.keywordGroups
                    textRole: "name"
                    valueRole: "id"
                    delegate: darkComboDelegate
                    onActivated: keywordGroupDialog.loadManageGroup(currentValue)
                    contentItem: Text { text: parent.displayText || "请选择关键词组"; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight }
                    background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                }
                AppButton {
                    Layout.preferredWidth: 92
                    text: "新建词组"
                    onClicked: keywordGroupDialog.startNewGroup()
                    contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                    background: Rectangle { radius: 7; color: parent.hovered ? "#263e63" : window.panel2; border.color: parent.hovered ? window.blue : window.line }
                }
            }
            RowLayout { Layout.fillWidth: true
                Text { text: "适用平台"; color: window.muted; Layout.preferredWidth: 92; font.pixelSize: 11 }
                ComboBox { id: keywordGroupPlatformChooser; Layout.fillWidth: true; model: [{label: "抖音", value: "douyin"}, {label: "小红书", value: "xhs"}, {label: "B站", value: "bilibili"}, {label: "微博", value: "weibo"}, {label: "快手", value: "kuaishou"}]; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate; contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true
                Text { text: "词组名称"; color: window.muted; Layout.preferredWidth: 92; font.pixelSize: 11 }
                TextField { id: keywordGroupNameField; Layout.fillWidth: true; color: window.ink; palette.placeholderText: window.muted; placeholderText: "例如：洗护行业客户"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            Text { text: "关键词设置（多个词可用顿号、逗号、分号或换行分隔）"; color: window.muted; font.pixelSize: 11 }
            RowLayout { Layout.fillWidth: true
                Text { text: "核心词"; color: window.muted; Layout.preferredWidth: 92; font.pixelSize: 11 }
                TextArea { id: keywordGroupCoreField; Layout.fillWidth: true; Layout.preferredHeight: 58; color: window.ink; palette.placeholderText: window.muted; placeholderText: "必填，例如：京东洗衣、互联网洗衣"; wrapMode: TextArea.Wrap; selectByMouse: true; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true
                Text { text: "同义词"; color: window.muted; Layout.preferredWidth: 92; font.pixelSize: 11 }
                TextArea { id: keywordGroupSynonymField; Layout.fillWidth: true; Layout.preferredHeight: 48; color: window.ink; palette.placeholderText: window.muted; placeholderText: "可选，例如：洗衣服务、衣物护理"; wrapMode: TextArea.Wrap; selectByMouse: true; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true
                Text { text: "地区词"; color: window.muted; Layout.preferredWidth: 92; font.pixelSize: 11 }
                TextArea { id: keywordGroupRegionField; Layout.fillWidth: true; Layout.preferredHeight: 42; color: window.ink; palette.placeholderText: window.muted; placeholderText: "可选，例如：河南、郑州"; wrapMode: TextArea.Wrap; selectByMouse: true; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true
                Text { text: "排除词"; color: window.muted; Layout.preferredWidth: 92; font.pixelSize: 11 }
                TextArea { id: keywordGroupExcludeField; Layout.fillWidth: true; Layout.preferredHeight: 42; color: window.ink; palette.placeholderText: window.muted; placeholderText: "可选，不希望搜索的词"; wrapMode: TextArea.Wrap; selectByMouse: true; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            Item { Layout.fillHeight: true }
            RowLayout { Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                AppButton { text: "取消"; onClicked: keywordGroupDialog.close(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
                AppButton { text: keywordGroupDialog.manageMode && keywordGroupDialog.editingGroupId > 0 ? "保存修改" : "保存关键词组"; enabled: keywordGroupNameField.text.trim().length > 0 && (keywordGroupCoreField.text.trim().length > 0 || keywordGroupSynonymField.text.trim().length > 0); onClicked: { if (keywordGroupDialog.manageMode && keywordGroupDialog.editingGroupId > 0) backend.updateKeywordGroup(keywordGroupDialog.editingGroupId, keywordGroupNameField.text, String(keywordGroupPlatformChooser.currentValue || ""), keywordGroupCoreField.text, keywordGroupSynonymField.text, keywordGroupRegionField.text, keywordGroupExcludeField.text); else backend.createKeywordGroup(keywordGroupNameField.text, String(keywordGroupPlatformChooser.currentValue || "douyin"), keywordGroupCoreField.text, keywordGroupSynonymField.text, keywordGroupRegionField.text, keywordGroupExcludeField.text); keywordGroupDialog.close() } contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: parent.enabled ? window.blue : window.panel2 } }
            }
        }
    }

    }

    Dialog {
        id: logStatsDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(620, window.width - 80)
        height: Math.min(520, window.height - 80)
        property int alertIndex: 0
        function alertRows() { return backend.diagnosticLogStats.alerts || [] }
        function currentAlert() {
            var rows = alertRows()
            return rows.length ? rows[Math.min(alertIndex, rows.length - 1)] : null
        }
        function currentAlertText() {
            var row = currentAlert()
            if (!row) return "今天没有警告或错误"
            var details = row.details && Object.keys(row.details).length ? "\n详情：" + JSON.stringify(row.details) : ""
            return "[" + String(row.timestamp || "").slice(-12) + "] " + (row.message || "") + details
        }
        onOpened: alertIndex = 0
        title: "当天日志统计"
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 42; color: window.panel2; border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; text: logStatsDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
            AppButton { anchors.right: parent.right; anchors.rightMargin: 7; anchors.verticalCenter: parent.verticalCenter; width: 32; height: 32; text: "×"; onClicked: logStatsDialog.close(); contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 } background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" } }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: "仅统计今天（" + (backend.diagnosticLogStats.date || "") + "）的后台操作记录"; color: window.muted; font.pixelSize: 11 }
            RowLayout { Layout.fillWidth: true; spacing: 8
                Rectangle { Layout.fillWidth: true; height: 72; radius: 9; color: "#183452"; Text { anchors.centerIn: parent; text: "全部\n" + (backend.diagnosticLogStats.total || 0); color: window.blue; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 14 } }
                Rectangle { Layout.fillWidth: true; height: 72; radius: 9; color: "#173b37"; Text { anchors.centerIn: parent; text: "正常\n" + (backend.diagnosticLogStats.normal || 0); color: window.green; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 14 } }
                Rectangle { Layout.fillWidth: true; height: 72; radius: 9; color: "#493a20"; Text { anchors.centerIn: parent; text: "警告\n" + (backend.diagnosticLogStats.warning || 0); color: window.amber; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 14 } }
                Rectangle { Layout.fillWidth: true; height: 72; radius: 9; color: "#4a2830"; Text { anchors.centerIn: parent; text: "错误\n" + (backend.diagnosticLogStats.error || 0); color: "#ff9b9b"; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 14 } }
            }
            Rectangle { Layout.fillWidth: true; Layout.fillHeight: true; radius: 9; color: window.panel2; border.color: window.line
                ColumnLayout { anchors.fill: parent; anchors.margins: 14; spacing: 8
                    Text { text: "下一条警告 / 错误"; color: window.ink; font.pixelSize: 13; font.weight: Font.DemiBold }
                    Text { Layout.fillWidth: true; Layout.fillHeight: true; text: logStatsDialog.currentAlertText(); color: logStatsDialog.currentAlert() ? "#ffcf86" : window.muted; wrapMode: Text.WordWrap; font.pixelSize: 11 }
                    RowLayout { Layout.fillWidth: true; Item { Layout.fillWidth: true }
                        AppButton { text: "下一条"; enabled: logStatsDialog.alertRows().length > 0; onClicked: { logStatsDialog.alertIndex = (logStatsDialog.alertIndex + 1) % logStatsDialog.alertRows().length } contentItem: Text { text: parent.text; color: parent.enabled ? window.amber : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 7; color: "transparent"; border.color: parent.enabled ? window.amber : window.line } }
                        AppButton { text: "刷新日志"; onClicked: { backend.refreshDiagnostics(); logStatsDialog.alertIndex = 0 } contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 7; color: window.panel; border.color: parent.hovered ? window.blue : window.line } }
                    }
                }
            }
        }
    }

    Dialog {
        id: accountDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(520, window.width - 80)
        height: 390
        title: "添加账号"
        function selectedWindowIsOpen() {
            if (String(accountPlatformChooser.currentValue || "") === "tieba") return true
            var selected = String(accountWindowField.text || "").trim()
            if (!selected) return false
            for (var i = 0; i < backend.diagnosticBitBrowserWindows.length; i++) {
                var row = backend.diagnosticBitBrowserWindows[i] || {}
                if (String(row.id || "") === selected) return Boolean(row.opened)
            }
            return false
        }
        function windowStatusText() {
            if (String(accountPlatformChooser.currentValue || "") === "tieba") return "贴吧使用 API 令牌，不需要 BitBrowser 窗口"
            var selected = String(accountWindowField.text || "").trim()
            if (!selected) return "请先在比特浏览器创建并打开窗口，再从列表选择"
            return selectedWindowIsOpen() ? "已确认窗口正在打开，可保存" : "窗口未打开或尚未刷新，请先打开后点击刷新"
        }
        function openForWindow(row) {
            var platform = String(row && row.platform || "douyin")
            var platformIndex = 0
            for (var i = 0; i < accountPlatformChooser.count; i++) {
                accountPlatformChooser.currentIndex = i
                if (String(accountPlatformChooser.currentValue || "") === platform) {
                    platformIndex = i
                    break
                }
            }
            accountPlatformChooser.currentIndex = platformIndex
            accountNameField.text = ""
            accountWindowField.text = String(row && row.window_id || "")
            accountWindowChooser.currentIndex = -1
            accountDialog.open()
        }
        onOpened: backend.inspectBitBrowser()
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 42
            color: window.panel2
            border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; text: accountDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: "普通平台需选择已打开的 BitBrowser 窗口；贴吧使用 API 令牌，不需要窗口。"; color: window.amber; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
            RowLayout { Layout.fillWidth: true; Text { text: "账号名称"; color: window.muted; Layout.preferredWidth: 90; font.pixelSize: 11 }
                TextField { id: accountNameField; Layout.fillWidth: true; color: window.ink; palette.placeholderText: window.muted; placeholderText: "例如：抖音主账号"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "平台"; color: window.muted; Layout.preferredWidth: 90; font.pixelSize: 11 }
                ComboBox { id: accountPlatformChooser; Layout.fillWidth: true; model: [{label: "抖音", value: "douyin"}, {label: "小红书", value: "xhs"}, {label: "B站", value: "bilibili"}, {label: "微博", value: "weibo"}, {label: "快手", value: "kuaishou"}, {label: "百度贴吧", value: "tieba"}]; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate; onActivated: accountWindowField.text = ""; contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true; Text { text: "窗口 ID"; color: window.muted; Layout.preferredWidth: 90; font.pixelSize: 11 }
                ColumnLayout { Layout.fillWidth: true; spacing: 6
                    RowLayout { Layout.fillWidth: true
                        TextField { id: accountWindowField; Layout.fillWidth: true; color: window.ink; palette.placeholderText: window.muted; placeholderText: "从下方已打开窗口中选择"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                        AppButton { text: "刷新窗口"; onClicked: backend.inspectBitBrowser(); contentItem: Text { text: parent.text; color: window.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 } background: Rectangle { radius: 7; color: window.panel2; border.color: parent.hovered ? window.blue : window.line } }
                    }
                    ComboBox { id: accountWindowChooser; Layout.fillWidth: true; model: backend.diagnosticBitBrowserWindows; textRole: "name"; valueRole: "id"; delegate: darkComboDelegate; onActivated: accountWindowField.text = String(currentValue || ""); contentItem: Text { text: parent.currentIndex >= 0 && parent.displayText ? parent.displayText : "选择已打开的 BitBrowser 窗口"; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
                    Text { text: accountDialog.windowStatusText(); color: accountDialog.selectedWindowIsOpen() ? window.green : window.muted; font.pixelSize: 10; Layout.fillWidth: true; elide: Text.ElideRight }
                }
            }
            Item { Layout.fillHeight: true }
            RowLayout { Layout.fillWidth: true; Item { Layout.fillWidth: true }
                AppButton { text: "取消"; onClicked: accountDialog.close(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
                AppButton { text: "保存账号"; enabled: accountNameField.text.trim().length > 0 && accountDialog.selectedWindowIsOpen(); onClicked: { backend.addAccount(accountNameField.text, String(accountPlatformChooser.currentValue || "douyin"), accountWindowField.text); accountDialog.close() } contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: parent.enabled ? window.blue : window.panel2 } }
            }
        }
    }

    Dialog {
        id: createBrowserDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(560, window.width - 80)
        height: 300
        title: "创建浏览器窗口"
        property var platformOptions: [
            {label: "抖音", value: "douyin"},
            {label: "小红书", value: "xhs"},
            {label: "B站", value: "bilibili"},
            {label: "微博", value: "weibo"},
            {label: "快手", value: "kuaishou"}
        ]
        onOpened: {
            var current = String(accountPlatformFilter.currentValue || "")
            var selected = 0
            for (var i = 0; i < platformOptions.length; i++) {
                if (platformOptions[i].value === current) { selected = i; break }
            }
            createBrowserPlatformChooser.currentIndex = selected
            createBrowserNameField.text = ""
        }
        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 42
            color: window.panel2
            border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 48; text: createBrowserDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
            AppButton {
                anchors.right: parent.right; anchors.rightMargin: 7; anchors.verticalCenter: parent.verticalCenter
                width: 32; height: 32; text: "×"
                onClicked: createBrowserDialog.close()
                contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 }
                background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" }
            }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: "创建后自动打开窗口；登录平台后，再到“添加账号”中选择窗口并保存。"; color: window.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
            RowLayout { Layout.fillWidth: true
                Text { text: "平台"; color: window.muted; Layout.preferredWidth: 82; font.pixelSize: 11 }
                ComboBox { id: createBrowserPlatformChooser; Layout.fillWidth: true; model: createBrowserDialog.platformOptions; textRole: "label"; valueRole: "value"; delegate: darkComboDelegate; contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9 } background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            RowLayout { Layout.fillWidth: true
                Text { text: "窗口名称"; color: window.muted; Layout.preferredWidth: 82; font.pixelSize: 11 }
                TextField { id: createBrowserNameField; Layout.fillWidth: true; color: window.ink; palette.placeholderText: window.muted; placeholderText: "可留空，自动按平台和时间命名"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            Item { Layout.fillHeight: true }
            RowLayout { Layout.fillWidth: true; Item { Layout.fillWidth: true }
                AppButton { text: "取消"; onClicked: createBrowserDialog.close(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
                AppButton { text: "创建并打开"; onClicked: { backend.createBrowserWindow(String(createBrowserPlatformChooser.currentValue || "douyin"), createBrowserNameField.text); createBrowserDialog.close() } contentItem: Text { text: parent.text; color: "#071224"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 7; color: window.blue } }
            }
        }
    }

    Dialog {
        id: browserWindowDeleteDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(560, window.width - 80)
        title: "确认删除浏览器窗口"
        background: Rectangle { radius: 14; color: window.panel; border.color: "#8b4d5a" }
        header: Rectangle {
            implicitHeight: 44
            color: "#3a1f2b"
            Text { anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 48; text: browserWindowDeleteDialog.title; color: "#ffd9df"; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
            AppButton {
                anchors.right: parent.right; anchors.rightMargin: 7; anchors.verticalCenter: parent.verticalCenter
                width: 32; height: 32; text: "×"
                onClicked: browserWindowDeleteDialog.reject()
                contentItem: Text { text: parent.text; color: parent.hovered ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 21 }
                background: Rectangle { radius: 7; color: parent.hovered ? "#263952" : "transparent" }
            }
        }
        contentItem: ColumnLayout {
            spacing: 10
            Text { text: "确定删除窗口“" + accountsPage.deleteCandidateWindowName + "”吗？"; color: window.ink; font.pixelSize: 13; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            Text { text: "窗口 ID：" + accountsPage.deleteCandidateWindowId; color: window.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            Text { text: "这会先关闭窗口，再永久删除 BitBrowser profile，操作不可恢复。只有未绑定账号的窗口可以删除；已绑定窗口请先解除绑定。"; color: window.amber; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
        }
        footer: RowLayout {
            spacing: 8
            Item { Layout.fillWidth: true }
            AppButton { text: "取消"; onClicked: browserWindowDeleteDialog.reject(); contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 7; color: "transparent"; border.color: window.line } }
            AppButton { text: "确认删除"; onClicked: accountsPage.confirmDeleteWindow(); contentItem: Text { text: parent.text; color: "#fff1f3"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 } background: Rectangle { radius: 7; color: "#b95768"; border.color: "#e97583" } }
        }
    }

    Dialog {
        id: templateDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(780, window.width - 80)
        height: Math.min(700, window.height - 80)
        title: "回复模板与变量"
        ListModel { id: variableModel }
        property bool saving: false
        property string saveStatus: ""
        property bool saveStatusOk: false

        function loadCustomVariables() {
            variableModel.clear()
            for (var i = 0; i < backend.interactionCustomVariables.length; i++) {
                var item = backend.interactionCustomVariables[i]
                variableModel.append({"name": String(item.name || ""), "value": String(item.value || "")})
            }
        }

        function newTemplate() {
            dialogTemplateChooser.currentIndex = -1
            templateIdField.text = ""
            templateContentField.text = ""
            // 新建模板时保留已有的全局变量，避免保存新模板把旧变量
            // 意外清空；用户仍可在这里继续添加、编辑或移除变量。
            loadCustomVariables()
            saveStatus = "正在新建模板：填写名称、完整回复内容后保存"
            saveStatusOk = true
        }

        function loadSelectedTemplate() {
            var item = dialogTemplateChooser.currentIndex >= 0
                    ? backend.interactionTemplates[dialogTemplateChooser.currentIndex] : null
            if (!item) return
            templateIdField.text = String(item.id || "")
            templateContentField.text = String(item.content || "")
        }

        onOpened: {
            saving = false
            saveStatus = ""
            loadCustomVariables()
            if (dialogTemplateChooser.count > 0) {
                dialogTemplateChooser.currentIndex = 0
                loadSelectedTemplate()
            }
        }

        background: Rectangle { radius: 14; color: window.panel; border.color: window.line }
        header: Rectangle {
            implicitHeight: 42
            color: window.panel2
            border.color: window.line
            Text { anchors.fill: parent; anchors.leftMargin: 16; text: templateDialog.title; color: window.ink; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.weight: Font.DemiBold }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: "模板正文会完整保存；修改模板不会改写已经进入待发送或已回复的历史内容。"; color: window.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true; font.pixelSize: 11 }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "选择模板"; color: window.muted; font.pixelSize: 11; Layout.preferredWidth: 76 }
                ComboBox {
                    id: dialogTemplateChooser
                    Layout.fillWidth: true
                    model: backend.interactionTemplates
                    textRole: "content"
                    valueRole: "id"
                    delegate: darkComboDelegate
                    onActivated: templateDialog.loadSelectedTemplate()
                    contentItem: Text { text: parent.displayText; color: window.ink; verticalAlignment: Text.AlignVCenter; leftPadding: 9; elide: Text.ElideRight; font.pixelSize: 11 }
                    background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
                }
                AppButton {
                    text: "＋ 新建模板"
                    Layout.preferredWidth: 94
                    Layout.preferredHeight: 32
                    onClicked: templateDialog.newTemplate()
                    contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                    background: Rectangle { radius: 7; color: parent.hovered ? "#203b61" : "transparent"; border.color: parent.hovered ? window.blue : window.line }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "模板名称"; color: window.muted; font.pixelSize: 11; Layout.preferredWidth: 76 }
                TextField { id: templateIdField; Layout.fillWidth: true; color: window.ink; palette.placeholderText: window.muted; placeholderText: "例如：首轮跟进"; background: Rectangle { radius: 7; color: window.panel2; border.color: window.line } }
            }
            Text { text: "完整回复内容"; color: window.muted; font.pixelSize: 11 }
            TextArea {
                id: templateContentField
                Layout.fillWidth: true
                Layout.preferredHeight: 138
                color: window.ink
                wrapMode: TextArea.Wrap
                selectByMouse: true
                palette.placeholderText: window.muted
                placeholderText: "请输入完整回复话术，可使用下方中文变量"
                background: Rectangle { radius: 7; color: window.panel2; border.color: window.line }
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "可用自定义变量"; color: window.muted; font.pixelSize: 11 }
                Item { Layout.fillWidth: true }
                AppButton {
                    text: "＋ 添加变量"
                    onClicked: variableModel.append({"name": "", "value": ""})
                    contentItem: Text { text: parent.text; color: window.blue; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                    background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
                }
            }
            ListView {
                id: variableList
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(150, Math.max(38, variableModel.count * 38))
                clip: true
                model: variableModel
                delegate: RowLayout {
                    width: variableList.width
                    height: 34
                    spacing: 7
                    TextField {
                        Layout.preferredWidth: 180
                        text: model.name
                        color: window.ink
                        palette.placeholderText: window.muted
                        placeholderText: "变量名称，如门店名称"
                        onTextChanged: if (activeFocus) variableModel.setProperty(index, "name", text)
                        background: Rectangle { radius: 6; color: window.panel2; border.color: window.line }
                    }
                    TextField {
                        Layout.fillWidth: true
                        text: model.value
                        color: window.ink
                        palette.placeholderText: window.muted
                        placeholderText: "替换内容"
                        onTextChanged: if (activeFocus) variableModel.setProperty(index, "value", text)
                        background: Rectangle { radius: 6; color: window.panel2; border.color: window.line }
                    }
                    Text { text: "{{" + model.name + "}}"; color: window.blue; Layout.preferredWidth: 130; elide: Text.ElideRight; font.pixelSize: 10 }
                    AppButton {
                        text: "移除"
                        onClicked: variableModel.remove(index)
                        contentItem: Text { text: parent.text; color: "#ff9b9b"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 10 }
                        background: Rectangle { radius: 6; color: "transparent"; border.color: window.line }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "系统变量：{{昵称}}、{{平台}}、{{省份}}、{{城市}}、{{产品}}、{{联系方式提示}}"; color: window.muted; font.pixelSize: 10; elide: Text.ElideRight; Layout.fillWidth: true }
                Text { text: templateDialog.saveStatus; color: templateDialog.saveStatusOk ? window.green : window.amber; font.pixelSize: 10; elide: Text.ElideRight; Layout.preferredWidth: 250 }
                AppButton {
                    text: "取消"
                    enabled: !templateDialog.saving
                    onClicked: templateDialog.close()
                    contentItem: Text { text: parent.text; color: window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                    background: Rectangle { radius: 7; color: "transparent"; border.color: window.line }
                }
                AppButton {
                    text: templateDialog.saving ? "保存中…" : "保存模板"
                    enabled: !templateDialog.saving && templateIdField.text.trim().length > 0 && templateContentField.text.trim().length > 0
                    onClicked: {
                        var variables = []
                        for (var i = 0; i < variableModel.count; i++)
                            variables.push(variableModel.get(i))
                        templateDialog.saving = true
                        templateDialog.saveStatus = "正在校验并保存…"
                        templateDialog.saveStatusOk = true
                        backend.saveTemplate(templateIdField.text, templateContentField.text, variables)
                    }
                    contentItem: Text { text: parent.text; color: parent.enabled ? "#071224" : "#536681"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 11 }
                    background: Rectangle { radius: 7; color: parent.enabled ? window.blue : window.panel2; border.color: window.line }
                }
            }
        }
    }
}
}
