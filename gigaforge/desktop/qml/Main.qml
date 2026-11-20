import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    visible: true
    width: 1540
    height: 980
    minimumWidth: 1120
    minimumHeight: 740
    title: "GigaForge · agent workspace"
    color: themeTokens.paper

    property string activePage: bridge.startPage
    property bool contextVisible: bridge.contextPanelVisible
    property bool terminalWindowOpen: false
    property bool chatWindowOpen: false
    property bool chatHistoryVisible: true
    property Item navigationHoverTarget: null
    property string navigationHoverText: ""
    property string adaptiveWorkspaceIntent: "general"
    property string appliedWorkspaceIntent: "general"
    property bool adaptiveWorkspaceSnapshotValid: false
    property bool adaptiveContextBefore: true
    property bool adaptiveHistoryBefore: true
    property bool modelPickerExpanded: false
    readonly property var modelPickerPrimaryChoices: {
        const available = bridge.modelChoices
        const preferred = [
            "GigaChat-3-Ultra",
            "GigaChat-2-Max",
            "GigaChat-2-Pro",
            "GigaChat-2-Lite"
        ]
        const primary = []
        for (let i = 0; i < preferred.length && primary.length < 4; i++) {
            for (let j = 0; j < available.length; j++) {
                if (String(available[j]).toLowerCase() === preferred[i].toLowerCase()) {
                    primary.push(available[j])
                    break
                }
            }
        }
        for (let i = 0; i < available.length && primary.length < 4; i++) {
            if (primary.indexOf(available[i]) === -1)
                primary.push(available[i])
        }
        return primary
    }
    readonly property var modelPickerAdditionalChoices: {
        const additional = []
        for (let i = 0; i < bridge.modelChoices.length; i++) {
            if (modelPickerPrimaryChoices.indexOf(bridge.modelChoices[i]) === -1)
                additional.push(bridge.modelChoices[i])
        }
        return additional
    }
    readonly property var modelPickerChoices: modelPickerExpanded
        ? modelPickerPrimaryChoices.concat(modelPickerAdditionalChoices)
        : modelPickerPrimaryChoices
    readonly property var agentProfileChoices: [
        { id: "", name: window.tr("Стандартный агент", "Default agent") }
    ].concat(bridge.agentProfiles)
    property var terminalWindowGeometry: bridge.floatingTerminalGeometry
    property var chatWindowGeometry: bridge.floatingChatGeometry
    property real floatingWindowZCounter: 120
    property real terminalWindowZ: 121
    property real chatWindowZ: 122
    readonly property color ink: themeTokens.ink
    readonly property color inkSoft: themeTokens.graphite
    readonly property color ocean: themeTokens.ember
    readonly property color mint: themeTokens.verified
    readonly property real densityScale: bridge.density === "compact" ? 0.88 : 1.0
    readonly property real textScale: 1.1 * Math.max(1.0, Math.min(1.2, width / 1280.0))
                                      * (bridge.density === "compact" ? 0.96 : 1.0)
    readonly property string displayRunStatus: {
        const raw = String(bridge.lastRunStatus || "").toLowerCase()
        const labels = {
            succeeded: window.tr("Завершено", "Completed"),
            success: window.tr("Завершено", "Completed"),
            completed: window.tr("Завершено", "Completed"),
            running: window.tr("Выполняется", "Running"),
            failed: window.tr("Ошибка", "Failed"),
            cancelled: window.tr("Остановлено", "Cancelled"),
            unknown: window.tr("Пока нет запусков", "No runs yet"),
            "ожидание": window.tr("Пока нет запусков", "No runs yet")
        }
        return labels[raw] || bridge.lastRunStatus
    }
    readonly property real displayProgressPercent: {
        if (bridge.running)
            return Math.max(0, Math.min(100, Number(bridge.progressPercent) || 0))
        const raw = String(bridge.lastRunStatus || "").toLowerCase()
        return ["success", "succeeded", "completed", "done"].indexOf(raw) >= 0 ? 100 : 0
    }
    readonly property string displayProgress: bridge.running
        ? bridge.progressPercent + "%"
        : displayProgressPercent > 0 ? Math.round(displayProgressPercent) + "%" : "—"
    readonly property int contextRunTokens: bridge ? Math.max(0, Number(bridge.runTokenTotal) || 0) : 0
    readonly property int contextTaskBudget: bridge ? Math.max(0, Number(bridge.taskTokenBudget) || 0) : 0
    readonly property string displayStatusText: {
        const raw = String(bridge.statusText || "")
        if (lang !== "en")
            return raw
        const labels = {
            "Готово к работе": "Ready",
            "Новый разговор": "New conversation",
            "Подключаю агентов…": "Starting agents…",
            "Цикл завершён": "Run completed",
            "Цель завершена · результат проверен": "Goal completed · result verified",
            "Цель заблокирована · требуется внимание": "Goal blocked · attention required",
            "Цель остановлена · результат требует проверки": "Goal stopped · result needs verification",
            "Цель остановлена до завершения": "Goal stopped before completion",
            "Цель завершилась с ошибкой": "Goal failed",
            "Цикл Goal остановлен до подтверждения результата": "Goal stopped before the result was verified",
            "Цикл Goal завершён · проверьте итоговый статус": "Goal cycle ended · check the final status",
            "Выполняется действие": "Running action",
            "Ошибка выполнения": "Execution error",
            "Введите GigaChat credential для сохранения": "Enter a GigaChat credential to save",
            "Текущий runtime не поддерживает смену credentials": "The current runtime cannot change credentials",
            "Проверяю подключение и сохраняю настройки…": "Checking connection and saving settings…",
            "Настройки сохранены · каталог моделей пока недоступен": "Settings saved · model catalog is not available yet",
            "Изменения будут применяться автоматически": "Changes will be applied automatically",
            "Изменения требуют подтверждения": "Changes require approval",
            "Нельзя менять профиль во время выполнения задачи": "Profiles cannot be changed while a task is running",
            "Выбранный профиль агента не найден": "The selected agent profile was not found",
            "Имя профиля должно содержать от 1 до 60 символов": "Profile names must contain 1 to 60 characters",
            "Инструкции профиля должны содержать от 1 до 4000 символов": "Profile instructions must contain 1 to 4000 characters",
            "Редактируемый профиль агента не найден": "The agent profile being edited was not found",
            "Удаляемый профиль агента не найден": "The agent profile to delete was not found"
        }
        const fileCount = raw.match(/^Файлы проекта · (\d+) элементов$/)
        if (fileCount)
            return "Project files · " + fileCount[1] + " items"
        const modelCount = raw.match(/^Подключено · найдено моделей: (\d+) · настройки сохранены$/)
        if (modelCount)
            return "Connected · " + modelCount[1] + " models found · settings saved"
        const runtimeError = raw.match(/^Завершено с ошибкой: (.*)$/)
        if (runtimeError)
            return "Stopped with an error: " + runtimeError[1]
        const credentialError = raw.match(/^Настройки активны, но сохранение\/проверка завершились с ошибкой: (.*)$/)
        if (credentialError)
            return "Settings are active, but saving or verification failed: " + credentialError[1]
        const credentialWarning = raw.match(/^Настройки сохранены, проверка API не прошла: (.*)$/)
        if (credentialWarning)
            return "Settings saved, but the API check failed: " + credentialWarning[1]
        return labels[raw] || raw
    }
    readonly property string lang: bridge.language
    function tr(ru, en) { return lang === "en" ? en : ru }
    function setNavigationHover(target, label, active) {
        if (active) {
            navigationHoverTarget = target
            navigationHoverText = label
        } else if (navigationHoverTarget === target) {
            navigationHoverTarget = null
            navigationHoverText = ""
        }
    }
    function workspaceIntentLabel(intent) {
        const labels = {
            writing: window.tr("Текст", "Writing"),
            codebase: window.tr("Кодовая база", "Codebase"),
            automation: window.tr("Автоматизация", "Automation")
        }
        return labels[String(intent || "")] || window.tr("Общий", "General")
    }
    function workspaceIntentReason(intent) {
        const reasons = {
            writing: window.tr(
                "Скрыть историю и живой контекст; режим выполнения не меняется.",
                "Hide history and live context; the execution mode stays unchanged."
            ),
            codebase: window.tr(
                "Показать файлы проекта, историю и живой контекст.",
                "Show project files, history, and live context."
            ),
            automation: window.tr(
                "Открыть живой контекст и ход запуска по реальным событиям.",
                "Show live context and execution progress from actual events."
            )
        }
        return reasons[String(intent || "")] || ""
    }
    function applyAdaptiveWorkspace(intent) {
        if (intent === "general")
            return
        if (!adaptiveWorkspaceSnapshotValid) {
            adaptiveContextBefore = contextVisible
            adaptiveHistoryBefore = chatHistoryVisible
            adaptiveWorkspaceSnapshotValid = true
        }
        if (intent === "writing") {
            contextVisible = false
            chatHistoryVisible = false
        } else if (intent === "codebase") {
            contextVisible = true
            chatHistoryVisible = true
        } else if (intent === "automation") {
            contextVisible = true
            chatHistoryVisible = false
        }
        appliedWorkspaceIntent = intent
    }
    function restoreAdaptiveWorkspace() {
        if (adaptiveWorkspaceSnapshotValid) {
            contextVisible = adaptiveContextBefore
            chatHistoryVisible = adaptiveHistoryBefore
            bridge.setContextPanelVisible(adaptiveContextBefore)
        }
        adaptiveWorkspaceSnapshotValid = false
        appliedWorkspaceIntent = "general"
        adaptiveWorkspaceIntent = "general"
    }
    function suggestAdaptiveWorkspace(text) {
        if (activePage !== "forge" || !String(text || "").trim())
            return
        const intent = bridge.classifyWorkspaceIntent(text)
        if (intent === "general") {
            if (adaptiveWorkspaceIntent !== "general")
                restoreAdaptiveWorkspace()
            return
        }
        adaptiveWorkspaceIntent = intent
        if (bridge.adaptiveWorkspaceEnabled)
            applyAdaptiveWorkspace(intent)
    }
    Timer {
        id: workspaceIntentTimer
        interval: 450
        repeat: false
        onTriggered: window.suggestAdaptiveWorkspace(composer.text)
    }
    function formatCount(value) {
        const count = Math.max(0, Math.floor(Number(value) || 0))
        const separator = lang === "en" ? "," : "\u00a0"
        return String(count).replace(/\B(?=(\d{3})+(?!\d))/g, separator)
    }
    function goalRuntimeStatusLabel(status) {
        const raw = String(status || "")
        if (lang !== "en")
            return raw
        const labels = {
            "Offline preview · установка runtime отключена": "Offline preview · runtime setup is disabled",
            "Проверяю компоненты Goal и Chromium…": "Checking Goal components and Chromium…",
            "Среда Goal готова · тестовый runtime и Chromium проверены": "Goal environment ready · test runtime and Chromium verified",
            "Среда Goal ещё не проверена": "Goal environment has not been checked yet",
            "Среда Goal не готова": "Goal environment is not ready"
        }
        if (labels[raw])
            return labels[raw]
        const issue = raw.match(/^Среда Goal требует внимания · (.*)$/)
        return issue ? "Goal environment needs attention · " + issue[1] : raw
    }
    function statusLabel(status) {
        const raw = String(status || "").toLowerCase()
        const labels = {
            succeeded: window.tr("Завершено", "Completed"),
            success: window.tr("Завершено", "Completed"),
            completed: window.tr("Завершено", "Completed"),
            running: window.tr("Выполняется", "Running"),
            failed: window.tr("Ошибка", "Failed"),
            cancelled: window.tr("Остановлено", "Cancelled"),
            pending: window.tr("Ожидание", "Waiting")
        }
        return labels[raw] || String(status || "")
    }
    function agentRoleLabel(agent) {
        if (agent.group === "subagent")
            return window.tr("Субагент текущей задачи", "Subagent in current task")
        return lang === "en" ? String(agent.role || "")
            : String(agent.roleRu || agent.role || "")
    }
    function agentStatusLabel(status) {
        const raw = String(status || "")
        if (lang !== "en")
            return raw
        const labels = {
            "В каталоге": "In catalog",
            "Работает": "Working",
            "Завершил": "Finished",
            "Ошибка": "Error",
            "Есть ошибки": "Has errors",
            "Нет результата": "No result",
            "Состояние неизвестно": "Unknown state"
        }
        return labels[raw] || raw
    }
    readonly property var recentActivity: {
        const rows = bridge.events
        let taskStart = 0
        let hasCurrentTask = false
        for (let i = 0; i < rows.length; i++) {
            const event = rows[i]
            const data = event.data || {}
            if (event.kind === "command" && data.role === "user") {
                taskStart = i
                hasCurrentTask = true
            }
        }
        const activity = rows.slice(taskStart).filter(function(event) {
            return ["step", "tool", "error"].indexOf(String(event.kind || "")) >= 0
        })
        if (activity.length || hasCurrentTask || bridge.running)
            return activity.slice(-8).reverse()
        return bridge.lastSteps.slice(-8).reverse().map(function(step) {
            return {
                kind: "step",
                data: { node: step.node, status: step.status },
                text: String(step.ms || 0) + " ms"
                    + (step.attempts > 1 ? " · " + step.attempts + " attempts" : "")
            }
        })
    }
    readonly property bool hasCurrentTask: {
        const rows = bridge.events
        for (let i = 0; i < rows.length; i++) {
            const event = rows[i]
            if (event.kind === "command" && (event.data || {}).role === "user")
                return true
        }
        return bridge.running
    }
    readonly property string contextTaskTitle: hasCurrentTask
        ? window.tr("Текущая задача", "Current task")
        : bridge.lastSteps.length > 0
            ? window.tr("Последняя задача", "Last task")
            : window.tr("Задача", "Task")
    readonly property string contextActivityTitle: hasCurrentTask
        ? window.tr("Ход задачи", "Task activity")
        : bridge.lastSteps.length > 0
            ? window.tr("Последний запуск", "Last run")
            : window.tr("Активность", "Activity")
    readonly property string contextTaskText: {
        const rows = bridge.events
        for (let i = rows.length - 1; i >= 0; i--) {
            const event = rows[i]
            if (event.kind === "command" && (event.data || {}).role === "user")
                return String(event.text || "")
        }
        const lastGoal = String(bridge.lastGoal || "")
        return !lastGoal || lastGoal === "Пока нет активных задач"
            ? window.tr("Пока нет активных задач", "No active tasks yet")
            : lastGoal
    }
    readonly property string contextTaskStatus: hasCurrentTask
        ? window.displayStatusText : window.displayRunStatus
    readonly property var agentCatalogStats: {
        const groups = {}
        let roles = 0
        bridge.agents.forEach(function(agent) {
            if (agent.group === "subagent" || agent.stage === "runtime")
                return
            roles++
            groups[String(agent.group || "core")] = true
        })
        return { roles: roles, families: Object.keys(groups).length }
    }
    readonly property var forgeStarters: {
        if (bridge.mode === "agent") {
            return [
                { icon: "inspect", tint: themeTokens.plate,
                  title: window.tr("Изучить проект", "Inspect the project"),
                  detail: window.tr("Архитектура, риски, приоритеты", "Architecture, risks, priorities"),
                  prompt: window.tr(
                      "Изучи проект, объясни его устройство и предложи три приоритетных улучшения. Пока ничего не меняй.",
                      "Inspect the project, explain its structure, and suggest three priority improvements. Do not edit files yet.") },
                { icon: "create", tint: themeTokens.emberTint,
                  title: window.tr("Реализовать задачу", "Implement a task"),
                  detail: window.tr("Изменения с проверкой результата", "Changes with verification"),
                  prompt: window.tr(
                      "Реализуй следующую возможность: [опиши результат]. Изучи соответствующий код, внеси необходимые изменения и запусти подходящие проверки.",
                      "Implement this capability: [describe the outcome]. Inspect the relevant code, make the necessary changes, and run appropriate checks.") },
                { icon: "quality", tint: "#F1E6CC",
                  title: window.tr("Проверить качество", "Check quality"),
                  detail: window.tr("Сборка, тесты, найденные ошибки", "Build, tests, and discovered issues"),
                  prompt: window.tr(
                      "Проведи проверку проекта: найди доступные тесты и команды сборки, запусти их и исправь обнаруженные ошибки. В конце перечисли проверки и их результат.",
                      "Check the project: find available tests and build commands, run them, and fix any failures. Summarize the checks and their results.") }
            ]
        }
        if (bridge.mode === "goal") {
            return [
                { icon: "inspect", tint: themeTokens.plate,
                  title: window.tr("Задать результат", "Define an outcome"),
                  detail: window.tr("Цель и проверяемые критерии", "Goal and measurable acceptance criteria"),
                  prompt: window.tr(
                      "Цель: [опиши измеримый результат]. Сначала изучи проект, затем составь план, реализуй изменения и проверь каждый критерий приёмки.",
                      "Goal: [describe a measurable outcome]. Inspect the project, plan and implement the changes, then verify each acceptance criterion.") },
                { icon: "create", tint: themeTokens.emberTint,
                  title: window.tr("Улучшить продукт", "Improve the product"),
                  detail: window.tr("Работа циклами до проверки", "Iterate and verify the result"),
                  prompt: window.tr(
                      "Цель: улучши [часть продукта] так, чтобы [измеримый критерий успеха]. Работай циклами, проверяй результат и остановись после подтверждения критерия.",
                      "Goal: improve [part of the product] so that [measurable success criterion]. Iterate, verify the result, and stop once the criterion is met.") },
                { icon: "quality", tint: "#F1E6CC",
                  title: window.tr("Устранить дефект", "Resolve a defect"),
                  detail: window.tr("Исправление с повторной проверкой", "Fix and re-run relevant checks"),
                  prompt: window.tr(
                      "Цель: исправь дефект [опиши проблему] и добейся прохождения относящихся к нему проверок. Не обходи тесты и не ослабляй критерии качества.",
                      "Goal: fix this defect [describe the problem] and get the relevant checks passing. Do not bypass tests or weaken quality criteria.") }
            ]
        }
        return [
            { icon: "inspect", tint: themeTokens.aquaTint,
              title: window.tr("Работа с кодом", "Code work"),
              detail: window.tr("Реализация и проверка изменений", "Implement and verify changes"),
              prompt: window.tr(
                  "Изучи связанный код и реализуй задачу: [опиши желаемый результат]. Запусти подходящие проверки и сообщи об изменённых файлах.",
                  "Inspect the relevant code and implement this task: [describe the outcome]. Run suitable checks and summarize the changed files.") },
            { icon: "quality", tint: "#E5EDFF",
              title: window.tr("Автоматизация", "Automation"),
              detail: window.tr("Повторяемые задачи без рутины", "Turn repetitive tasks into a flow"),
              prompt: window.tr(
                  "Помоги автоматизировать процесс: [опиши повторяемую задачу]. Изучи проект, предложи минимальное надёжное решение и проверь его.",
                  "Help automate this workflow: [describe the recurring task]. Inspect the project, propose the smallest reliable solution, and verify it.") },
            { icon: "inspect", tint: "#E5F4ED",
              title: window.tr("Исследование", "Research"),
              detail: window.tr("Разобраться в проекте или идее", "Explore a project or idea"),
              prompt: window.tr(
                  "Исследуй вопрос: [опиши тему]. Изучи доступные файлы и источники, отдели факты от предположений и представь выводы со ссылками.",
                  "Research this question: [describe the topic]. Inspect available files and sources, distinguish facts from assumptions, and present linked findings.") },
            { icon: "create", tint: "#F1E6CC",
              title: window.tr("Новый агент", "New agent"),
              detail: window.tr("Специалист для отдельной роли", "A specialist for a focused role"),
              prompt: window.tr(
                  "Спроектируй агента для роли [опиши специализацию]: определи его задачи, границы ответственности, необходимые инструменты и критерии качества. Пока не меняй файлы.",
                  "Design an agent for this role [describe the specialty]: define its tasks, boundaries, tools, and quality criteria. Do not edit files yet.") }
        ]
    }
    readonly property string projectCrumb: {
        const raw = String(bridge.projectPath || "").replace(/\\/g, "/")
        const parts = raw.split("/").filter(function (part) { return part.length > 0 })
        if (parts.length >= 2)
            return parts[parts.length - 2] + " / " + parts[parts.length - 1]
        return bridge.projectName
    }
    readonly property string pageEyebrow: (lang === "en" ? ({
        overview: "Project overview",
        forge: "Chat · conversations",
        files: "Files · project",
        agents: "Agents · roles",
        monitoring: "Monitoring · system",
        explore: "Explore · deep research",
        orchestrate: "Orchestrate · execution",
        settings: "Settings · configuration"
    }) : ({
        overview: "Обзор проекта",
        forge: "Чат · диалоги",
        files: "Файлы · проект",
        agents: "Агенты · роли",
        monitoring: "Мониторинг · система",
        explore: "Explore · глубокое исследование",
        orchestrate: "Orchestrate · выполнение",
        settings: "Настройки · конфигурация"
    }))[activePage] || ""
    readonly property string pageTitle: (lang === "en" ? ({
        overview: "Workspace overview",
        forge: "Chat",
        files: "Project library",
        agents: "A team for the task",
        monitoring: "System health",
        explore: "Deep Research",
        orchestrate: "Execution workspace",
        settings: "Settings"
    }) : ({
        overview: "Рабочее пространство",
        forge: "Чат",
        files: "Библиотека проекта",
        agents: "Команда под задачу",
        monitoring: "Состояние системы",
        explore: "Deep Research",
        orchestrate: "Рабочее место запуска",
        settings: "Настройки"
    }))[activePage] || ""
    readonly property string pageSubtitle: (lang === "en" ? ({
        overview: "A clear view of project health, active work and where to continue.",
        forge: "",
        files: "Edits and a shell — without leaving the window.",
        agents: "Catalog: " + window.agentCatalogStats.roles + " roles · "
            + window.agentCatalogStats.families + " families. Live specialists show their latest state.",
        monitoring: "Connection, agent activity, resource usage and real runtime events.",
        explore: "",
        orchestrate: "Inspect the execution trace, its participants and the latest runtime events.",
        settings: "Connection, appearance, file changes and goal cycles. Applied without a restart."
    }) : ({
        overview: "Состояние проекта, актуальная работа и быстрый путь к продолжению.",
        forge: "",
        files: "Правки в проекте и консоль — без выхода из окна.",
        agents: "Каталог: " + window.agentCatalogStats.roles + " ролей · семейств: "
            + window.agentCatalogStats.families + ". Показаны последние состояния специалистов.",
        monitoring: "Подключение, активность агентов, ресурсы и реальные события runtime.",
        explore: "",
        orchestrate: "Ход конкретного запуска, его участники и последние события runtime.",
        settings: "Подключение, оформление, правки файлов и циклы цели. Без перезапуска."
    }))[activePage] || ""

    ThemeTokens {
        id: themeTokens
        themeName: bridge.appearanceTheme
        light: bridge.isLightTheme
        compact: bridge.density === "compact"
        motion: bridge.motionEnabled
    }

    Component.onCompleted: bridge.refreshFiles()

    function formatDiff(value) {
        let raw = String(value)
        if (lang === "en") {
            const labels = {
                "Выберите файл проекта, чтобы посмотреть изменения.": "Select a project file to view changes.",
                "Изменений с момента открытия файла нет.": "No changes since the file was opened."
            }
            raw = labels[raw] || raw
        }
        return raw.split("\n").map(function(line) {
            const escaped = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            const color = line.startsWith("@@") ? themeTokens.graphite
                        : line.startsWith("+++") || line.startsWith("---") ? themeTokens.ink3
                        : line.startsWith("+") ? themeTokens.verified
                        : line.startsWith("-") ? themeTokens.fail : themeTokens.ink
            return "<font color=\"" + color + "\">" + (escaped || " ") + "</font>"
        }).join("<br/>")
    }

    function goalStatusLabel(status) {
        const raw = String(status || "")
        const labelsRu = {
            running: "В работе",
            needs_attention: "Нужно продолжить",
            blocked: "Нужно внимание",
            failed: "Ошибка",
            cancelled: "Остановлена",
            completed: "Завершена",
            paused: "Приостановлена"
        }
        const labelsEn = {
            running: "Running",
            needs_attention: "Needs continuation",
            blocked: "Needs attention",
            failed: "Failed",
            cancelled: "Cancelled",
            completed: "Completed",
            paused: "Paused"
        }
        const labels = lang === "en" ? labelsEn : labelsRu
        return labels[raw] || window.tr("Сохранена", "Saved")
    }

    function navigateTo(page, moveFocus) {
        activePage = page
        if (moveFocus === false)
            return
        Qt.callLater(function() {
            if (page === "forge") {
                composer.forceActiveFocus()
            } else if (page === "files") {
                fileSearch.forceActiveFocus()
            } else if (page === "explore") {
                explorePage.focusSearch()
            } else {
                const sectionOrder = ["overview", "monitoring", "settings"]
                const index = sectionOrder.indexOf(page)
                const item = index >= 0 ? topNavigationItems.itemAt(index) : null
                if (item)
                    item.forceActiveFocus()
            }
        })
    }

    function cycleAppearanceTheme() {
        const themes = ["pearl", "graphite", "verdant"]
        const current = themes.indexOf(bridge.appearanceTheme)
        bridge.setAppearanceTheme(themes[(current + 1) % themes.length])
    }

    function toggleTerminalWindow() {
        terminalWindowOpen = !terminalWindowOpen
        if (terminalWindowOpen)
            Qt.callLater(function() { terminalQuickCommand.forceActiveFocus() })
    }

    function toggleChatWindow() {
        chatWindowOpen = !chatWindowOpen
        if (chatWindowOpen)
            Qt.callLater(function() { floatingChatComposer.forceActiveFocus() })
    }

    function raiseFloatingWindow(kind) {
        floatingWindowZCounter += 1
        if (kind === "chat")
            chatWindowZ = floatingWindowZCounter
        else
            terminalWindowZ = floatingWindowZCounter
    }

    function sendFloatingChatPrompt() {
        let prompt = floatingChatComposer.text.trim()
        if (!prompt && bridge.attachments.length)
            prompt = window.tr("Проанализируйте прикреплённые файлы.", "Analyze the attached files.")
        if (!prompt || bridge.running)
            return
        if (bridge.mode === "goal" && !bridge.goalRuntimeReady) {
            if (!bridge.goalRuntimeRunning)
                bridge.startGoalRuntimeSetup()
            return
        }
        bridge.submitPrompt(prompt)
        floatingChatComposer.clear()
    }

    function runCommand(command) {
        commandPalette.close()
        if (command === "newChat") {
            bridge.newChat()
            navigateTo("forge", true)
        } else if (command === "focusTask") {
            navigateTo("forge", true)
        } else if (command === "theme") {
            cycleAppearanceTheme()
        } else if (command === "toggleChat") {
            toggleChatWindow()
        } else if (command === "toggleTerminal") {
            toggleTerminalWindow()
        } else if (command === "help") {
            keyboardHelpPopup.open()
        } else {
            navigateTo(command, false)
        }
    }

    function sendPrompt() {
        let prompt = composer.text.trim()
        if (!prompt && bridge.attachments.length)
            prompt = window.tr("Проанализируйте прикреплённые файлы.", "Analyze the attached files.")
        if (!prompt || bridge.running)
            return
        if (bridge.mode === "goal" && !bridge.goalRuntimeReady) {
            if (!bridge.goalRuntimeRunning)
                bridge.startGoalRuntimeSetup()
            return
        }
        bridge.submitPrompt(prompt)
        composer.clear()
        activePage = "forge"
    }

    RoundedHoverLabel {
        id: navigationHoverLabel
        anchorItem: window.navigationHoverTarget
        label: window.navigationHoverText
        hoverActive: window.navigationHoverTarget !== null
        textScale: window.textScale
        densityScale: window.densityScale
    }

    Shortcut {
        sequence: "Ctrl+1"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("overview")
    }
    Shortcut {
        sequence: "Ctrl+2"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("forge")
    }
    Shortcut {
        sequence: "Ctrl+3"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("explore")
    }
    Shortcut {
        sequence: "Ctrl+4"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("orchestrate")
    }
    Shortcut {
        sequence: "Ctrl+5"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("files")
    }
    Shortcut {
        sequence: "Ctrl+6"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("agents")
    }
    Shortcut {
        sequence: "Ctrl+7"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("monitoring")
    }
    Shortcut {
        sequence: "Ctrl+8"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("settings")
    }
    Shortcut {
        sequence: "Ctrl+K"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: commandPalette.open()
    }
    Shortcut {
        sequence: "Ctrl+L"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("forge")
    }
    Shortcut {
        sequence: "Ctrl+Shift+J"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.toggleTerminalWindow()
    }
    Shortcut {
        sequence: "Ctrl+Shift+C"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.toggleChatWindow()
    }
    Shortcut {
        sequence: "Ctrl+Shift+F"
        enabled: !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: window.navigateTo("files")
    }
    Shortcut {
        sequence: "Ctrl+N"
        enabled: !bridge.running && !goalHistoryDialog.visible && !keyboardHelpPopup.visible
        onActivated: {
            bridge.newChat()
            window.navigateTo("forge")
        }
    }
    Shortcut {
        sequence: "Ctrl+/"
        enabled: !goalHistoryDialog.visible
        onActivated: keyboardHelpPopup.open()
    }

    background: Item {
        Rectangle {
            anchors.fill: parent
            color: themeTokens.field
        }
        Image {
            anchors.fill: parent
            visible: themeTokens.light
            source: "../../assets/watercolor-wash-v3.png"
            fillMode: Image.PreserveAspectCrop
            opacity: 0.22
            smooth: true
        }
        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: chatHistoryPanel.width
            color: themeTokens.rail
        }
        Canvas {
            id: auroraCanvas
            anchors.fill: parent
            renderStrategy: Canvas.Cooperative
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                var theme = bridge.appearanceTheme
                function blob(x, y, r, color) {
                    var g = ctx.createRadialGradient(x, y, 0, x, y, r)
                    g.addColorStop(0, color)
                    g.addColorStop(1, "rgba(255,255,255,0)")
                    ctx.fillStyle = g
                    ctx.fillRect(x - r, y - r, r * 2, r * 2)
                }
                if (theme === "pearl") {
                    blob(width * 0.86, height * 0.02, width * 0.43, "rgba(18,166,196,0.11)")
                    blob(width * 0.02, height * 0.38, width * 0.36, "rgba(15,165,127,0.08)")
                    blob(width * 0.56, height * 1.02, width * 0.40, "rgba(31,91,224,0.045)")
                    blob(width * 0.93, height * 0.67, width * 0.32, "rgba(15,165,127,0.06)")
                    ctx.strokeStyle = "rgba(11,27,36,0.022)"
                } else if (theme === "graphite") {
                    blob(width * 0.85, height * 0.02, width * 0.35, "rgba(45,212,191,0.06)")
                    blob(width * 0.03, height * 0.72, width * 0.32, "rgba(109,141,255,0.055)")
                    blob(width * 0.68, height * 1.02, width * 0.34, "rgba(183,166,255,0.045)")
                    ctx.strokeStyle = "rgba(150,176,196,0.052)"
                } else {
                    blob(width * 0.88, height * 0.03, width * 0.42, "rgba(52,211,153,0.085)")
                    blob(width * 0.03, height * 0.75, width * 0.34, "rgba(94,234,212,0.06)")
                    blob(width * 0.62, height * 1.02, width * 0.36, "rgba(124,140,255,0.055)")
                    blob(width * 0.37, height * 0.12, width * 0.25, "rgba(240,194,123,0.04)")
                    ctx.strokeStyle = "rgba(134,239,195,0.018)"
                }
                ctx.lineWidth = 1
                if (theme === "graphite") {
                    for (var gx = 0; gx <= width; gx += 56) {
                        ctx.beginPath()
                        ctx.moveTo(gx, 0)
                        ctx.lineTo(gx, height)
                        ctx.stroke()
                    }
                    for (var gy = 0; gy <= height; gy += 56) {
                        ctx.beginPath()
                        ctx.moveTo(0, gy)
                        ctx.lineTo(width, gy)
                        ctx.stroke()
                    }
                }
            }
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
        }
        Connections {
            target: bridge
            function onStateChanged() { auroraCanvas.requestPaint() }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 0
        spacing: 0 * window.densityScale

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 54

            Rectangle {
                anchors.fill: parent
                gradient: Gradient {
                    GradientStop { position: 0.0; color: themeTokens.topbarTop }
                    GradientStop { position: 1.0; color: themeTokens.topbarBottom }
                }
            }
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: chatHistoryPanel.width
                color: themeTokens.rail
            }
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: themeTokens.line
            }

            RowLayout {
                objectName: "topbarNavigationRow"
                anchors.fill: parent
                anchors.leftMargin: 8 * window.densityScale
                anchors.rightMargin: 22
                spacing: 8 * window.densityScale

                Item {
                    objectName: "topbarBrandMark"
                    Layout.preferredWidth: 38
                    Layout.preferredHeight: 38
                    Image {
                        anchors.fill: parent
                        source: "../../assets/gigaforge-mark.png"
                        sourceSize.width: 38
                        sourceSize.height: 38
                        smooth: true
                    }
                }
                Repeater {
                    id: topNavigationItems
                    model: [
                        { key: "overview", label: "Обзор", labelEn: "Overview", glyph: "\uE80F", shortcut: "Ctrl+1" },
                        { key: "monitoring", label: "Мониторинг", labelEn: "Monitoring", glyph: "\uE9D9", shortcut: "Ctrl+7" },
                        { key: "settings", label: "Настройки", labelEn: "Settings", glyph: "\uE713", shortcut: "Ctrl+8" }
                    ]
                    delegate: Button {
                        id: topNavigationButton
                        required property var modelData
                        readonly property bool navActive: window.activePage === modelData.key
                        objectName: "navigation-" + modelData.key
                        Layout.preferredWidth: 42
                        Layout.preferredHeight: 40
                        padding: 0
                        Accessible.name: (window.lang === "en" ? modelData.labelEn : modelData.label)
                            + " · " + modelData.shortcut
                        onHoveredChanged: window.setNavigationHover(
                            topNavigationButton, Accessible.name, hovered)
                        onClicked: window.navigateTo(modelData.key, false)
                        background: Rectangle {
                            radius: themeTokens.radiusControl
                            color: navActive ? themeTokens.navActive
                                : parent.hovered ? themeTokens.surface2 : "transparent"
                            border.color: navActive ? themeTokens.lineStrong : "transparent"
                        }
                        contentItem: Text {
                            text: modelData.glyph
                            color: navActive ? themeTokens.navGlow : themeTokens.railInk2
                            font.family: bridge.iconFontFamily
                            font.pixelSize: Math.round(21 * window.textScale)
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
                Rectangle {
                    Layout.preferredWidth: 1
                    Layout.preferredHeight: 24
                    color: themeTokens.line
                }

                Text {
                    text: window.projectCrumb
                    color: themeTokens.ink
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(11.5 * window.textScale)
                    elide: Text.ElideMiddle
                    Layout.maximumWidth: 320
                }
                Button {
                    objectName: "commandSearchButton"
                    visible: window.width > 1200
                    Layout.preferredWidth: 220
                    Layout.preferredHeight: 28
                    text: window.tr("Поиск и команды   Ctrl+K", "Search and commands   Ctrl+K")
                    onClicked: commandPalette.open()
                    background: Rectangle { radius: themeTokens.radiusPill; color: themeTokens.surface; border.color: themeTokens.line }
                    contentItem: Text { text: parent.text; color: themeTokens.ink3; font.family: themeTokens.text; font.pixelSize: Math.round(9.5 * window.textScale); horizontalAlignment: Text.AlignLeft; verticalAlignment: Text.AlignVCenter; leftPadding: 12 }
                }
                Item { Layout.fillWidth: true }
                Rectangle {
                    width: 6
                    height: 6
                    radius: 3
                    color: bridge.connected ? themeTokens.verified : themeTokens.warn
                }
                Text {
                    text: bridge.connectionStatus
                    color: themeTokens.graphite
                    font.pixelSize: Math.round(11 * window.textScale)
                    visible: window.width > 1420
                }
                Button {
                    id: modelSelectorButton
                    objectName: "modelSelectorButton"
                    Layout.preferredHeight: 28
                    Layout.preferredWidth: Math.min(164, Math.max(108, modelNameLabel.implicitWidth + 28))
                    Accessible.name: window.tr("Выбрать рабочую модель", "Choose the working model")
                    ToolTip.visible: hovered
                    ToolTip.text: window.tr("Рабочая модель", "Working model")
                    onClicked: modelPickerPopup.open()
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: parent.hovered ? themeTokens.surface2 : themeTokens.surface
                        border.color: parent.activeFocus ? themeTokens.primary : themeTokens.line
                    }
                    contentItem: RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 9
                        anchors.rightMargin: 8
                        spacing: 5
                        Text {
                            id: modelNameLabel
                            text: bridge.modelName
                            color: themeTokens.graphite
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(9.5 * window.textScale)
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Text {
                            text: "⌄"
                            color: themeTokens.primaryDeep
                            font.pixelSize: Math.round(12 * window.textScale)
                        }
                    }
                }
                Rectangle {
                    Layout.preferredHeight: 26
                    Layout.preferredWidth: 88
                    radius: themeTokens.radiusControl
                    color: themeTokens.surface3
                    border.color: themeTokens.line
                    border.width: 1

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 2
                        spacing: 2 * window.densityScale
                        Repeater {
                            model: [{ code: "ru", label: "RU" }, { code: "en", label: "EN" }]
                            delegate: Button {
                                required property var modelData
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                text: modelData.label
                                Accessible.name: modelData.code === "ru" ? "Русский интерфейс" : "English interface"
                                onClicked: bridge.setLanguage(modelData.code)
                                background: Rectangle {
                                    radius: 4
                                    color: bridge.language === modelData.code ? themeTokens.surface : "transparent"
                                }
                                contentItem: Text {
                                    text: parent.text
                                    color: bridge.language === modelData.code ? themeTokens.ink : themeTokens.mist
                                    font.family: themeTokens.display
                                    font.pixelSize: Math.round(9.5 * window.textScale)
                                    font.letterSpacing: 0.8
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                    }
                }
                Button {
                    objectName: "appearanceCycleButton"
                    Accessible.name: bridge.appearanceTheme === "pearl"
                        ? window.tr("Жемчужная тема · нажмите, чтобы сменить", "Pearl theme · click to change")
                        : bridge.appearanceTheme === "graphite"
                            ? window.tr("Графитовая тема · нажмите, чтобы сменить", "Graphite theme · click to change")
                            : window.tr("Тема живой зелени · нажмите, чтобы сменить", "Verdant theme · click to change")
                    ToolTip.visible: hovered
                    ToolTip.text: bridge.appearanceTheme === "pearl"
                        ? window.tr("Жемчужная тема", "Pearl theme")
                        : bridge.appearanceTheme === "graphite"
                            ? window.tr("Графитовая тема", "Graphite theme")
                            : window.tr("Тема живой зелени", "Verdant theme")
                    onClicked: window.cycleAppearanceTheme()
                    Layout.preferredWidth: 28
                    Layout.preferredHeight: 26
                    background: Rectangle { radius: themeTokens.radiusPill; color: themeTokens.surface; border.color: themeTokens.line }
                    contentItem: Item {
                        Rectangle {
                            anchors.centerIn: parent
                            width: 15
                            height: 15
                            radius: 8
                            border.color: themeTokens.lineStrong
                            border.width: 1
                            gradient: Gradient {
                                GradientStop {
                                    position: 0
                                    color: bridge.appearanceTheme === "pearl" ? "#D5F5E8"
                                        : bridge.appearanceTheme === "graphite" ? "#28333A" : "#0B2B22"
                                }
                                GradientStop {
                                    position: 1
                                    color: bridge.appearanceTheme === "pearl" ? "#83CEC0"
                                        : bridge.appearanceTheme === "graphite" ? "#2DD4BF" : "#34D399"
                                }
                            }
                        }
                    }
                }
                Button {
                    objectName: "keyboardHelpButton"
                    Accessible.name: window.tr("Показать сочетания клавиш", "Show keyboard shortcuts")
                    ToolTip.visible: hovered
                    ToolTip.text: window.tr("Сочетания клавиш · Ctrl+/", "Keyboard shortcuts · Ctrl+/")
                    text: "?"
                    onClicked: keyboardHelpPopup.open()
                    Layout.preferredWidth: 26
                    Layout.preferredHeight: 26
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: "transparent"
                        border.color: parent.activeFocus ? themeTokens.ember : themeTokens.controlLine
                        border.width: 1
                    }
                    contentItem: Text {
                        text: parent.text
                        color: parent.activeFocus ? themeTokens.ember : themeTokens.graphite
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(11 * window.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
                Button {
                    objectName: "floatingTerminalButton"
                    Accessible.name: window.tr("Открыть плавающую консоль", "Open floating terminal")
                    ToolTip.visible: hovered
                    ToolTip.text: window.tr("Консоль проекта · Ctrl+Shift+J", "Project terminal · Ctrl+Shift+J")
                    text: ">_"
                    onClicked: window.toggleTerminalWindow()
                    Layout.preferredWidth: 36
                    Layout.preferredHeight: 26
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: window.terminalWindowOpen ? themeTokens.primaryTint : themeTokens.surface
                        border.color: window.terminalWindowOpen ? themeTokens.lineStrong : themeTokens.controlLine
                    }
                    contentItem: Item {
                        Text {
                            anchors.centerIn: parent
                            text: parent.parent.text
                            color: bridge.terminalBusy ? themeTokens.amber : themeTokens.aqua
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(10 * window.textScale)
                            font.bold: true
                        }
                        Rectangle {
                            visible: bridge.terminalRunning
                            width: 5
                            height: 5
                            radius: 3
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.rightMargin: 2
                            anchors.topMargin: 2
                            color: bridge.terminalBusy ? themeTokens.amber : themeTokens.mint
                        }
                    }
                }
                Button {
                    objectName: "floatingChatButton"
                    Accessible.name: window.tr("Открыть плавающий чат", "Open floating chat")
                    ToolTip.visible: hovered
                    ToolTip.text: window.tr("Чат и задачи · Ctrl+Shift+C", "Chat and tasks · Ctrl+Shift+C")
                    text: "\uE8BD"
                    onClicked: window.toggleChatWindow()
                    Layout.preferredWidth: 30
                    Layout.preferredHeight: 26
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: window.chatWindowOpen ? themeTokens.primaryTint : themeTokens.surface
                        border.color: window.chatWindowOpen ? themeTokens.lineStrong : themeTokens.controlLine
                    }
                    contentItem: Text {
                        text: parent.text
                        color: themeTokens.aqua
                        font.family: "Segoe MDL2 Assets"
                        font.pixelSize: Math.round(13 * window.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
                Button {
                    objectName: "contextToggleButton"
                    enabled: window.width >= 1120
                    Accessible.name: window.contextVisible
                                    ? window.tr("Скрыть живой контекст", "Hide live context")
                                    : window.tr("Показать живой контекст", "Show live context")
                    ToolTip.visible: hovered
                    ToolTip.text: window.width < 1120
                                  ? window.tr("Живой контекст доступен при ширине окна от 1120 px", "Live context is available from 1120 px window width")
                                  : (window.contextVisible
                                  ? window.tr("Скрыть живой контекст", "Hide live context")
                                  : window.tr("Показать живой контекст", "Show live context"))
                    text: window.contextVisible
                        ? "›"
                        : (window.width >= 1320
                            ? window.tr("‹  Контекст", "‹  Context")
                            : "‹")
                    onClicked: {
                        window.contextVisible = !window.contextVisible
                        bridge.setContextPanelVisible(window.contextVisible)
                    }
                    Layout.preferredWidth: !window.contextVisible && window.width >= 1320 ? 88 : 28
                    Layout.preferredHeight: 28
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: parent.hovered ? themeTokens.surface2 : "transparent"
                        border.color: parent.activeFocus ? themeTokens.ember : themeTokens.controlLine
                        border.width: 1
                    }
                    contentItem: Text {
                        text: parent.text
                        color: themeTokens.graphite
                        font.pixelSize: Math.round((window.contextVisible ? 16 : 10.5) * window.textScale)
                        font.bold: !window.contextVisible
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }
        }

        SplitView {

            id: shellSplit
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal
            spacing: 0
            handle: Rectangle {
                implicitWidth: 4
                implicitHeight: 8
                color: "transparent"
                radius: 4
                Rectangle {
                    anchors.centerIn: parent
                    width: shellSplit.resizing ? 2 : 1
                    height: shellSplit.resizing ? 36 : 24
                    radius: 2
                    color: shellSplit.resizing ? ocean : themeTokens.rule
                }
            }
            onResizingChanged: {
                if (!resizing)
                    bridge.saveShellLayout(chatHistoryPanel.width, contextPane.width)
            }

            ChatHistoryPanel {
                id: chatHistoryPanel
                objectName: "chatHistoryPanel"
                SplitView.minimumWidth: window.chatHistoryVisible ? 212 : 38
                SplitView.preferredWidth: window.chatHistoryVisible
                    ? Math.max(212, Math.min(280, bridge.navigationPanelWidth)) : 38
                SplitView.maximumWidth: window.chatHistoryVisible ? 280 : 38
                SplitView.fillHeight: true
                language: window.lang
                textScale: Math.min(window.textScale, 1.15) * window.densityScale
                densityScale: window.densityScale
                activePage: window.activePage
                collapsed: !window.chatHistoryVisible
                editorContents: editor.text
                onChooseProjectRequested: projectFolderDialog.open()
                onNewChatRequested: {
                    bridge.newChat()
                    window.navigateTo("forge")
                }
                onNavigateRequested: function(page) { window.navigateTo(page, false) }
                onCollapseRequested: window.chatHistoryVisible = false
                onExpandRequested: window.chatHistoryVisible = true
                onHoverLabelChanged: function(target, label, active) {
                    window.setNavigationHover(target, label, active)
                }
            }

            ColumnLayout {
                id: mainWorkspacePane
                objectName: "mainWorkspacePane"
                SplitView.minimumWidth: 430
                SplitView.fillWidth: true
                SplitView.fillHeight: true
                spacing: 9 * window.densityScale

                Item {
                    id: pageHeading
                    Layout.fillWidth: true
                    Layout.leftMargin: 24 * window.densityScale
                    Layout.rightMargin: 24 * window.densityScale
                    Layout.topMargin: 12 * window.densityScale
                    Layout.preferredHeight: pageHeadingRow.implicitHeight
                        + (window.activePage === "overview" ? 24 : 0)

                    GlassCard {
                        anchors.fill: parent
                        visible: window.activePage === "overview"
                        glass: true
                        inset: 0
                    }

                    RowLayout {
                        id: pageHeadingRow
                        anchors.fill: parent
                        anchors.leftMargin: window.activePage === "overview" ? 12 : 0
                        anchors.rightMargin: window.activePage === "overview" ? 12 : 0
                        spacing: 18 * window.densityScale

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0 * window.densityScale
                            Text {
                                text: window.pageEyebrow
                                color: themeTokens.ink3
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(9.5 * window.textScale)
                                font.letterSpacing: 1.6
                                font.capitalization: Font.AllUppercase
                            }
                            Text {
                                text: window.pageTitle
                                textFormat: Text.RichText
                                color: themeTokens.ink
                                font.family: themeTokens.display
                                font.pixelSize: Math.round(32 * window.textScale)
                                font.bold: true
                                font.letterSpacing: -0.8
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                                Layout.topMargin: 5 * window.densityScale
                            }
                            Text {
                                text: window.pageSubtitle
                                color: themeTokens.ink2
                                font.pixelSize: Math.round(12.5 * window.textScale)
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                                Layout.topMargin: 5 * window.densityScale
                            }
                        }

                        ColumnLayout {
                            Layout.alignment: Qt.AlignTop
                            spacing: 6 * window.densityScale
                            Item {
                                id: overviewCoreStage
                                visible: window.activePage === "overview"
                                    && mainWorkspacePane.width >= 700
                                    && window.height >= 760
                                Layout.preferredWidth: mainWorkspacePane.width < 900 ? 92 : 126
                                Layout.preferredHeight: mainWorkspacePane.width < 900 ? 82 : 112
                                readonly property real activityEnergy: bridge.running
                                    ? 0.66 + Math.min(0.3, Number(bridge.progressPercent || 0) / 350)
                                    : bridge.terminalBusy ? 0.78
                                    : bridge.goalRuntimeRunning ? 0.48
                                    : bridge.terminalReady ? 0.28
                                    : bridge.lastSteps.length > 0 ? 0.22 : 0.12
                                LivingCore {
                                    objectName: "overviewLivingCore"
                                    anchors.fill: parent
                                    energy: overviewCoreStage.activityEnergy
                                    active: window.activePage === "overview"
                                    motionEnabled: bridge.motionEnabled
                                    accent: themeTokens.aqua
                                    secondary: themeTokens.ember
                                }
                            }
                            Button {
                                objectName: "overviewNewTaskButton"
                                visible: window.activePage === "overview"
                                text: window.tr("Новая задача", "New task")
                                Accessible.name: window.tr("Перейти к вводу новой задачи", "Focus the new task field")
                                onClicked: composer.forceActiveFocus()
                                Layout.preferredHeight: 32
                                background: Rectangle {
                                    radius: themeTokens.radiusControl
                                    gradient: Gradient {
                                        GradientStop { position: 0.0; color: themeTokens.primary }
                                        GradientStop { position: 1.0; color: themeTokens.primaryDeep }
                                    }
                                    border.color: parent.activeFocus ? themeTokens.ink : "transparent"
                                    border.width: parent.activeFocus ? 2 : 0
                                }
                                contentItem: Text {
                                    text: parent.text
                                    color: themeTokens.actionInk
                                    font.pixelSize: Math.round(11 * window.textScale)
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    leftPadding: 12
                                    rightPadding: 12
                                }
                            }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: 24 * window.densityScale
                    Layout.rightMargin: 24 * window.densityScale
                    Layout.topMargin: 8 * window.densityScale
                    spacing: 12 * window.densityScale
                    Rectangle { width: 5; height: 5; radius: 3; color: bridge.running ? themeTokens.aqua : themeTokens.mint }
                    Text {
                        text: window.tr("проект ", "project ") + bridge.projectName
                        color: themeTokens.ink3
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(9.5 * window.textScale)
                    }
                    Rectangle { width: 1; height: 10; color: themeTokens.line }
                    Text {
                        text: bridge.files.length + window.tr(" файлов", " files")
                        color: themeTokens.ink3
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(9.5 * window.textScale)
                    }
                    Rectangle { width: 1; height: 10; color: themeTokens.line }
                    Text {
                        text: bridge.agents.length + window.tr(" ролей", " roles")
                        color: themeTokens.ink3
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(9.5 * window.textScale)
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: window.displayRunStatus
                        color: themeTokens.ink4
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(9.5 * window.textScale)
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.leftMargin: 24 * window.densityScale
                    Layout.rightMargin: 24 * window.densityScale
                    Layout.topMargin: 8 * window.densityScale
                    height: 1
                    color: themeTokens.line
                }

                StackLayout {

                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.leftMargin: 20 * window.densityScale
                    Layout.rightMargin: 20 * window.densityScale
                    currentIndex: Math.max(0, ["overview", "forge", "agents", "monitoring", "settings", "files", "explore", "orchestrate"].indexOf(window.activePage))

                    Item {
                        id: overviewPage
                        clip: true
                        property var dashboardPanelOrder: bridge.dashboardPanelOrder
                        function moveDashboardPanel(from, to) {
                            const next = dashboardPanelOrder.slice()
                            const moved = next.splice(from, 1)[0]
                            next.splice(to, 0, moved)
                            bridge.setDashboardPanelOrder(next)
                        }
                        readonly property bool compactTaskLayout: width < 980 || window.height < 820

                        ScrollView {
                            id: overviewScroll
                            anchors.fill: parent
                            clip: true
                            contentWidth: availableWidth
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                            ColumnLayout {
                                width: overviewScroll.availableWidth
                                spacing: 12 * window.densityScale

                                // ── текущая задача ───────────────────────────────
                                Component {
                                    id: taskPanelComponent
                                GlassCard {
                                    anchors.fill: parent
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: overviewPage.compactTaskLayout ? 204 : 224
                                    inset: 0
                                    glass: true
                                    glassTint: themeTokens.mintGlass
                                    accent: bridge.running ? themeTokens.aqua
                                          : (window.displayProgressPercent >= 100 ? themeTokens.mintDeep : themeTokens.primary)

                                    ColumnLayout {
                                        anchors.fill: parent
                                        anchors.margins: overviewPage.compactTaskLayout ? 14 : 18
                                        spacing: 0 * window.densityScale

                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 12 * window.densityScale

                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                spacing: 3 * window.densityScale
                                                Text {
                                                    text: window.tr("АКТИВНАЯ РАБОТА", "ACTIVE WORK")
                                                    color: themeTokens.ink2
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    font.letterSpacing: 1.4
                                                }
                                                Text {
                                                    id: overviewTaskTitle
                                                    text: window.contextTaskText
                                                    color: themeTokens.ink
                                                    font.family: themeTokens.display
                                                    font.pixelSize: Math.round((overviewPage.compactTaskLayout ? 16 : 18) * window.textScale)
                                                    font.bold: true
                                                    font.letterSpacing: -0.3
                                                    wrapMode: Text.WordWrap
                                                    elide: Text.ElideRight
                                                    maximumLineCount: overviewPage.compactTaskLayout ? 3 : 2
                                                    Layout.fillWidth: true
                                                    Accessible.name: window.contextTaskText
                                                    ToolTip.visible: taskTitleHover.hovered
                                                    ToolTip.text: window.contextTaskText
                                                    ToolTip.delay: 600
                                                    HoverHandler {
                                                        id: taskTitleHover
                                                    }
                                                }
                                                Text {
                                                    text: bridge.projectName + " · " + bridge.files.length + window.tr(" файлов", " files")
                                                    visible: !overviewPage.compactTaskLayout
                                                    color: themeTokens.ink2
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    elide: Text.ElideRight
                                                    Layout.fillWidth: true
                                                }
                                            }

                                            Rectangle {
                                                readonly property bool good: !bridge.running && window.displayProgressPercent >= 100
                                                Layout.preferredHeight: 22
                                                Layout.preferredWidth: stateLabel.implicitWidth + 16
                                                radius: themeTokens.radiusControl
                                                color: bridge.running ? themeTokens.aquaTint : (good ? themeTokens.mintDeep : themeTokens.surface3)
                                                border.color: bridge.running ? "#BFE0E7" : (good ? themeTokens.mintDeep : themeTokens.line)
                                                border.width: 1
                                                Text {
                                                    id: stateLabel
                                                    anchors.centerIn: parent
                                                    text: bridge.running ? window.tr("ИДЁТ ВЫПОЛНЕНИЕ", "RUNNING")
                                                          : (window.displayProgressPercent >= 100 ? window.tr("ЗАВЕРШЕНО", "COMPLETED")
                                                          : window.tr("ОЖИДАНИЕ", "IDLE"))
                                                    color: bridge.running ? themeTokens.aquaInk : (window.displayProgressPercent >= 100 ? themeTokens.actionInk : themeTokens.ink3)
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    font.letterSpacing: 0.8
                                                }
                                            }
                                            Button {
                                                objectName: "overviewOpenRunButton"
                                                visible: bridge.running || bridge.lastSteps.length > 0
                                                text: window.tr("Ход запуска", "View run")
                                                onClicked: window.navigateTo("orchestrate", false)
                                                background: Rectangle {
                                                    radius: themeTokens.radiusPill
                                                    color: parent.hovered ? themeTokens.surface3 : "transparent"
                                                    border.color: themeTokens.line
                                                }
                                                contentItem: Text {
                                                    text: parent.text
                                                    color: themeTokens.primaryDeep
                                                    font.family: themeTokens.text
                                                    font.pixelSize: Math.round(9.5 * window.textScale)
                                                    horizontalAlignment: Text.AlignHCenter
                                                    verticalAlignment: Text.AlignVCenter
                                                    leftPadding: 8
                                                    rightPadding: 8
                                                }
                                            }
                                        }

                                        Text {
                                            text: window.displayStatusText
                                            visible: bridge.running
                                                || ["failed", "cancelled"].indexOf(
                                                    String(bridge.lastRunStatus || "").toLowerCase()) >= 0
                                            color: themeTokens.ink2
                                            font.pixelSize: Math.round(12 * window.textScale)
                                            wrapMode: Text.WordWrap
                                            maximumLineCount: 2
                                            elide: Text.ElideRight
                                            Layout.fillWidth: true
                                            Layout.topMargin: 8 * window.densityScale
                                        }

                                        Item {
                                            objectName: "overviewRunProgress"
                                            Layout.fillWidth: true
                                            visible: bridge.running || bridge.lastSteps.length > 0
                                            Layout.preferredHeight: visible ? 22 : 0
                                            Layout.topMargin: visible ? 6 * window.densityScale : 0
                                            Rectangle {
                                                anchors.left: parent.left
                                                anchors.right: parent.right
                                                anchors.top: parent.top
                                                height: 3
                                                radius: 2
                                                color: themeTokens.surface3
                                            }
                                            Rectangle {
                                                anchors.left: parent.left
                                                anchors.top: parent.top
                                                width: parent.width * window.displayProgressPercent / 100
                                                height: 3
                                                radius: 2
                                                color: window.displayProgressPercent >= 100 ? themeTokens.mint : themeTokens.aqua
                                            }
                                            Text {
                                                anchors.right: parent.right
                                                anchors.top: parent.top
                                                anchors.topMargin: 6
                                                text: window.displayProgress
                                                color: themeTokens.ink3
                                                font.family: themeTokens.mono
                                                font.pixelSize: Math.round(9 * window.textScale)
                                            }
                                        }

                                        RowLayout {
                                            visible: false
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true
                                            Layout.topMargin: overviewPage.compactTaskLayout ? 8 : 12
                                            spacing: 0 * window.densityScale

                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                Layout.fillHeight: true
                                                Layout.rightMargin: 14 * window.densityScale
                                                spacing: 3 * window.densityScale
                                                Text {
                                                    text: window.tr("РОЛИ", "ROLES")
                                                    color: themeTokens.ink2
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    font.letterSpacing: 1.2
                                                    Layout.bottomMargin: 2 * window.densityScale
                                                }
                                                Repeater {
                                                    model: bridge.agents.slice(0, overviewPage.compactTaskLayout ? 2 : 3)
                                                    delegate: RowLayout {
                                                        required property var modelData
                                                        Layout.fillWidth: true
                                                        Layout.preferredHeight: 22
                                                        spacing: 7 * window.densityScale
                                                        Rectangle {
                                                            width: 20
                                                            height: 20
                                                            radius: 3
                                                            color: themeTokens.surface2
                                                            border.color: themeTokens.line
                                                            border.width: 1
                                                            Text {
                                                                anchors.centerIn: parent
                                                                text: String(modelData.name || modelData.role || "?").slice(0, 2).toUpperCase()
                                                                color: themeTokens.ink2
                                                                font.family: themeTokens.display
                                                                font.pixelSize: Math.round(10 * window.textScale)
                                                                font.bold: true
                                                            }
                                                        }
                                                        Text {
                                                            text: String(modelData.name || modelData.role || "")
                                                            color: themeTokens.ink
                                                            font.family: themeTokens.display
                                                            font.pixelSize: Math.round(10.5 * window.textScale)
                                                            font.bold: true
                                                            elide: Text.ElideRight
                                                            Layout.fillWidth: true
                                                        }
                                                        Text {
                                                            text: window.agentStatusLabel(modelData.status)
                                                            color: themeTokens.ink2
                                                            font.family: themeTokens.mono
                                                            font.pixelSize: Math.round(10 * window.textScale)
                                                        }
                                                    }
                                                }
                                                Text {
                                                    visible: bridge.agents.length === 0
                                                    text: window.tr("Роли появятся после первого запуска.", "Roles appear after the first run.")
                                                    color: themeTokens.ink2
                                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                                }
                                            }

                                            Rectangle { width: 1; Layout.fillHeight: true; color: themeTokens.lineSoft }

                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                Layout.fillHeight: true
                                                Layout.leftMargin: 14 * window.densityScale
                                                Layout.rightMargin: 14 * window.densityScale
                                                spacing: 3 * window.densityScale
                                                Text {
                                                    text: window.tr("ПОСЛЕДНИЕ ШАГИ", "LATEST STEPS")
                                                    color: themeTokens.ink2
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    font.letterSpacing: 1.2
                                                    Layout.bottomMargin: 2 * window.densityScale
                                                }
                                                Repeater {
                                                    model: window.recentActivity.slice(0, overviewPage.compactTaskLayout ? 2 : 3)
                                                    delegate: RowLayout {
                                                        required property var modelData
                                                        Layout.fillWidth: true
                                                        Layout.preferredHeight: 22
                                                        spacing: 7 * window.densityScale
                                                        Rectangle {
                                                            width: 5
                                                            height: 5
                                                            radius: 3
                                                            color: modelData.kind === "error" ? themeTokens.fail
                                                                 : modelData.kind === "tool" ? themeTokens.aqua : themeTokens.mint
                                                        }
                                                        Text {
                                                            text: {
                                                                const data = modelData.data || {}
                                                                return String(data.node || data.name || data.summary || modelData.text || modelData.kind)
                                                            }
                                                            color: themeTokens.ink2
                                                            font.pixelSize: Math.round(10.5 * window.textScale)
                                                            elide: Text.ElideRight
                                                            Layout.fillWidth: true
                                                        }
                                                    }
                                                }
                                                Text {
                                                    visible: window.recentActivity.length === 0
                                                    text: window.tr("Шаги появятся здесь во время выполнения.", "Steps will appear here during a run.")
                                                    color: themeTokens.ink2
                                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                                }
                                            }

                                            Rectangle { width: 1; Layout.fillHeight: true; color: themeTokens.lineSoft }

                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                Layout.fillHeight: true
                                                Layout.leftMargin: 14 * window.densityScale
                                                spacing: 3 * window.densityScale
                                                Text {
                                                    text: window.tr("ПРОГОН", "RUN")
                                                    color: themeTokens.ink2
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    font.letterSpacing: 1.2
                                                }
                                                RowLayout {
                                                    Layout.fillWidth: true
                                                    Layout.topMargin: 1 * window.densityScale
                                                    spacing: 8 * window.densityScale
                                                    Text {
                                                        text: window.displayProgress
                                                        color: themeTokens.ink
                                                        font.family: themeTokens.mono
                                                        font.pixelSize: Math.round(21 * window.textScale)
                                                        font.letterSpacing: -0.6
                                                    }
                                                    Text {
                                                        text: window.displayRunStatus
                                                        color: themeTokens.ink2
                                                        font.pixelSize: Math.round(10.5 * window.textScale)
                                                        elide: Text.ElideRight
                                                        Layout.fillWidth: true
                                                    }
                                                }
                                                Rectangle {
                                                    Layout.fillWidth: true
                                                    height: 3
                                                    color: themeTokens.surface3
                                                    Rectangle {
                                                        height: 3
                                                        width: parent.width * window.displayProgressPercent / 100
                                                        color: window.displayProgressPercent >= 100 ? themeTokens.mint : themeTokens.aqua
                                                    }
                                                }
                                                Text {
                                                    text: window.tr("Событий: ", "Events: ") + bridge.events.length
                                                    color: themeTokens.ink2
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    Layout.topMargin: 4 * window.densityScale
                                                }
                                            }
                                        }
                                    }
                                }
                                }

                                // ── двери сред ──────────────────────────────────
                                Component {
                                    id: environmentPanelComponent
                                RowLayout {
                                    anchors.fill: parent
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 120
                                    spacing: 16 * window.densityScale

                                    Repeater {
                                        model: [
                                            { key: "forge", title: "Chat", ru: "диалоги", en: "conversations",
                                              bodyRu: "История разговоров и проекты — в одном пространстве чата.",
                                              bodyEn: "Project folders and conversation history in one chat workspace.",
                                              footRu: bridge.chatSessions.length + " чатов · " + bridge.projectName,
                                              footEn: bridge.chatSessions.length + " chats · " + bridge.projectName,
                                              ready: true },
                                            { key: "explore", title: "Explore", ru: "глубокое исследование", en: "deep research",
                                              bodyRu: "Параллельные поиски, граф ссылок и сохраняемая библиотека источников.",
                                              bodyEn: "Parallel searches, a source-link graph and a saved knowledge library.",
                                              footRu: bridge.researchTopics.length + " поисков · " + bridge.researchLibrary.length + " источников",
                                              footEn: bridge.researchTopics.length + " searches · " + bridge.researchLibrary.length + " sources",
                                              ready: true },
                                            { key: "orchestrate", title: "Orchestrate", ru: "координация", en: "coordination",
                                              bodyRu: "Измеренные узлы, реальные участники и события выполнения.",
                                              bodyEn: "Measured nodes, live participants and execution events.",
                                              footRu: bridge.lastSteps.length + " узлов трассы · "
                                                  + monitoringPage.runtimeEvents.length
                                                  + " событий выполнения",
                                              footEn: bridge.lastSteps.length + " trace nodes · "
                                                  + monitoringPage.runtimeEvents.length
                                                  + " runtime events",
                                              ready: true }
                                        ]
                                        delegate: GlassCard {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true
                                            inset: 0
                                            opacity: modelData.ready ? 1 : 0.62

                                            ColumnLayout {
                                                anchors.fill: parent
                                                anchors.margins: 14
                                                spacing: 0 * window.densityScale

                                                RowLayout {
                                                    Layout.fillWidth: true
                                                    spacing: 8 * window.densityScale
                                                    Text {
                                                        text: modelData.title
                                                        color: themeTokens.ink
                                                        font.family: themeTokens.display
                                                        font.pixelSize: Math.round(14 * window.textScale)
                                                        font.bold: true
                                                    }
                                                    Text {
                                                        text: window.lang === "en" ? modelData.en : modelData.ru
                                                        color: themeTokens.ink4
                                                        font.family: themeTokens.mono
                                                        font.pixelSize: Math.round(10 * window.textScale)
                                                        font.letterSpacing: 1
                                                    }
                                                    Item { Layout.fillWidth: true }
                                                    Rectangle {
                                                        width: 6
                                                        height: 6
                                                        radius: 3
                                                        color: modelData.key === "forge" ? themeTokens.mint
                                                              : modelData.key === "explore" ? themeTokens.aqua : themeTokens.amber
                                                    }
                                                }
                                                Text {
                                                    text: window.lang === "en" ? modelData.bodyEn : modelData.bodyRu
                                                    color: themeTokens.ink2
                                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                                    wrapMode: Text.WordWrap
                                                    maximumLineCount: 2
                                                    elide: Text.ElideRight
                                                    Layout.fillWidth: true
                                                    Layout.topMargin: 8 * window.densityScale
                                                }
                                                Item { Layout.fillHeight: true }
                                                Text {
                                                    text: window.lang === "en" ? modelData.footEn : modelData.footRu
                                                    color: themeTokens.ink4
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                }
                                            }

                                            MouseArea {
                                                anchors.fill: parent
                                                enabled: modelData.ready
                                                cursorShape: modelData.ready ? Qt.PointingHandCursor : Qt.ArrowCursor
                                                onClicked: window.navigateTo(modelData.key, false)
                                            }
                                        }
                                    }
                                }
                                }

                                // ── журнал и среда ──────────────────────────────
                                Component {
                                    id: activityPanelComponent
                                RowLayout {
                                    anchors.fill: parent
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 148
                                    spacing: 16 * window.densityScale

                                    GlassCard {
                                        Layout.fillWidth: true
                                        Layout.fillHeight: true
                                        inset: 0
                                        ColumnLayout {
                                            anchors.fill: parent
                                            anchors.margins: 14
                                            spacing: 0 * window.densityScale
                                            RowLayout {
                                                Layout.fillWidth: true
                                                spacing: 8 * window.densityScale
                                                Text {
                                                    text: window.tr("НЕДАВНЯЯ АКТИВНОСТЬ", "RECENT ACTIVITY")
                                                    color: themeTokens.ink3
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    font.letterSpacing: 1.4
                                                }
                                                Item { Layout.fillWidth: true }
                                                Text {
                                                    text: bridge.events.length
                                                    color: themeTokens.ink4
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                }
                                            }
                                            ListView {
                                                Layout.fillWidth: true
                                                Layout.fillHeight: true
                                                Layout.topMargin: 4 * window.densityScale
                                                clip: true
                                                spacing: 0 * window.densityScale
                                                visible: bridge.events.length > 0
                                                model: bridge.events.slice(-4).reverse()
                                                delegate: Item {
                                                    required property var modelData
                                                    width: ListView.view.width
                                                    height: 24
                                                    Rectangle {
                                                        anchors.left: parent.left
                                                        anchors.right: parent.right
                                                        anchors.bottom: parent.bottom
                                                        height: 1
                                                        color: themeTokens.lineSoft
                                                    }
                                                    RowLayout {
                                                        anchors.fill: parent
                                                        spacing: 8 * window.densityScale
                                                        Rectangle {
                                                            width: 5
                                                            height: 5
                                                            radius: 3
                                                            color: modelData.kind === "error" ? themeTokens.fail
                                                                 : modelData.kind === "tool" ? themeTokens.aqua
                                                                 : modelData.kind === "step" ? themeTokens.primary : themeTokens.ink4
                                                        }
                                                        Text {
                                                            text: String((modelData.data || {}).summary || modelData.text || "")
                                                            color: themeTokens.ink2
                                                            font.pixelSize: Math.round(10.5 * window.textScale)
                                                            elide: Text.ElideRight
                                                            Layout.fillWidth: true
                                                        }
                                                        Text {
                                                            text: String(modelData.kind || "")
                                                            color: themeTokens.ink4
                                                            font.family: themeTokens.mono
                                                            font.pixelSize: Math.round(10 * window.textScale)
                                                        }
                                                    }
                                                }
                                            }
                                            Text {
                                                visible: bridge.events.length === 0
                                                text: window.tr("События появятся после запуска задачи.\nЖурнал покажет шаги, инструменты и результат.", "Events appear after a task starts.\nThe log will show steps, tools and the result.")
                                                color: themeTokens.ink4
                                                font.pixelSize: Math.round(11 * window.textScale)
                                                horizontalAlignment: Text.AlignHCenter
                                                Layout.fillWidth: true
                                                Layout.fillHeight: true
                                                verticalAlignment: Text.AlignVCenter
                                            }
                                        }
                                    }

                                    GlassCard {
                                        Layout.preferredWidth: 300
                                        Layout.fillHeight: true
                                        inset: 0
                                        ColumnLayout {
                                            anchors.fill: parent
                                            anchors.margins: 14
                                            spacing: 0 * window.densityScale
                                            Text {
                                                text: window.tr("СРЕДА", "ENVIRONMENT")
                                                color: themeTokens.ink3
                                                font.family: themeTokens.mono
                                                font.pixelSize: Math.round(10 * window.textScale)
                                                font.letterSpacing: 1.4
                                                Layout.bottomMargin: 4 * window.densityScale
                                            }
                                            Repeater {
                                                model: [
                                                    { k: window.tr("проект", "project"), v: bridge.projectName },
                                                    { k: window.tr("файлов", "files"), v: String(bridge.files.length) },
                                                    { k: window.tr("ролей", "roles"), v: String(bridge.agents.length) },
                                                    { k: window.tr("модель", "model"), v: bridge.modelName },
                                                    { k: window.tr("правки", "edits"), v: bridge.approvalMode === "ask" ? window.tr("спрашивать", "ask") : window.tr("разрешать", "allow") }
                                                ]
                                                delegate: Item {
                                                    required property var modelData
                                                    Layout.fillWidth: true
                                                    Layout.preferredHeight: 20
                                                    Rectangle {
                                                        anchors.left: parent.left
                                                        anchors.right: parent.right
                                                        anchors.bottom: parent.bottom
                                                        height: 1
                                                        color: themeTokens.lineSoft
                                                    }
                                                    RowLayout {
                                                        anchors.fill: parent
                                                        spacing: 8 * window.densityScale
                                                        Text {
                                                            text: modelData.k
                                                            color: themeTokens.ink4
                                                            font.family: themeTokens.mono
                                                            font.pixelSize: Math.round(10 * window.textScale)
                                                        }
                                                        Item { Layout.fillWidth: true }
                                                        Text {
                                                            text: modelData.v
                                                            color: themeTokens.ink2
                                                            font.family: themeTokens.mono
                                                            font.pixelSize: Math.round(10 * window.textScale)
                                                            elide: Text.ElideRight
                                                            Layout.maximumWidth: 180
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                                }
                            
                                Repeater {
                                    model: overviewPage.dashboardPanelOrder
                                    delegate: Item {
                                        id: movablePanel
                                        required property string modelData
                                        required property int index
                                        property int orderIndex: index
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: modelData === "task"
                                            ? (overviewPage.compactTaskLayout ? 280 : 312)
                                            : modelData === "environments" ? 120 : 148
                                        opacity: panelDrag.active ? 0.88 : 1.0

                                        Drag.active: panelDrag.active
                                        Drag.source: movablePanel
                                        Drag.keys: ["gigaforge-dashboard-panel"]
                                        Drag.supportedActions: Qt.MoveAction
                                        Drag.hotSpot.x: width / 2
                                        Drag.hotSpot.y: 16

                                        Loader {
                                            anchors.fill: parent
                                            sourceComponent: movablePanel.modelData === "task" ? taskPanelComponent
                                                : movablePanel.modelData === "environments" ? environmentPanelComponent
                                                : activityPanelComponent
                                        }

                                        DropArea {
                                            anchors.fill: parent
                                            keys: ["gigaforge-dashboard-panel"]
                                            onDropped: function(drop) {
                                                if (drop.source && drop.source.orderIndex !== movablePanel.orderIndex)
                                                    overviewPage.moveDashboardPanel(drop.source.orderIndex, movablePanel.orderIndex)
                                            }
                                        }

                                        Rectangle {
                                            anchors.right: parent.right
                                            anchors.top: parent.top
                                            anchors.rightMargin: 10
                                            anchors.topMargin: 8
                                            width: 28
                                            height: 24
                                            z: 10
                                            radius: themeTokens.radiusPill
                                            color: panelHandleHover.hovered || panelDrag.active ? themeTokens.surface3 : themeTokens.surface
                                            border.color: themeTokens.line
                                            Accessible.name: window.tr("Перетащить панель", "Drag to move panel")
                                            ToolTip.visible: panelHandleHover.hovered
                                            ToolTip.text: window.tr("Перетащите, чтобы изменить порядок панелей", "Drag to reorder dashboard panels")
                                            Grid {
                                                anchors.centerIn: parent
                                                columns: 2
                                                spacing: 3
                                                Repeater {
                                                    model: 6
                                                    delegate: Rectangle {
                                                        required property int index
                                                        width: 3
                                                        height: 3
                                                        radius: 2
                                                        color: panelHandleHover.hovered || panelDrag.active ? themeTokens.primary : themeTokens.ink3
                                                    }
                                                }
                                            }
                                            HoverHandler {
                                                id: panelHandleHover
                                                cursorShape: panelDrag.active ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                                            }
                                            DragHandler {
                                                id: panelDrag
                                                target: null
                                                onActiveChanged: movablePanel.Drag.active = active
                                            }
                                        }
                                    }
                                }
}
                        }
                    }
                    Item {
                        GlassCard {
                            anchors.fill: parent
                            inset: 16
                            ColumnLayout {
                                anchors.fill: parent
                                spacing: 10 * window.densityScale
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 9 * window.densityScale
                                    Rectangle { width: 36
                                        height: 36
                                        radius: themeTokens.radius
                                        color: themeTokens.emberTint
                                        Text {
                        anchors.centerIn: parent
                                        text: bridge.windowsIconFontAvailable ? "\uE943" : "C"
                                        font.family: bridge.iconFontFamily
                                        color: ocean
                                        font.pixelSize: Math.round(17 * window.textScale) } }
                                    ColumnLayout {
                                        spacing: 1 * window.densityScale
                                        Text {
                        font.family: themeTokens.text
                        text: window.tr("ДИАЛОГ", "CONVERSATION")
                                            color: ink
                                            font.pixelSize: Math.round(14 * window.textScale)
                                            font.bold: true }
                                        Text {
                        font.family: themeTokens.text
                        text: bridge.running ? window.displayStatusText
                            : bridge.mode === "chat" ? window.tr("История проекта и новый диалог", "Project history and new conversations")
                            : bridge.mode === "agent" ? window.tr("Один цикл разработки", "Single development cycle")
                            : window.tr("Последовательные циклы до результата", "Iterate until the outcome is verified")
                                            color: inkSoft
                                            font.pixelSize: Math.round(10.5 * window.textScale)
                                            elide: Text.ElideRight
                                            Layout.fillWidth: true }
                                    }
                                    Item { Layout.fillWidth: true }
                                    Text {
                        font.family: themeTokens.text
                        text: bridge.approvalMode === "ask"
                            ? window.tr("Ask · подтверждение правок", "Ask · approve edits")
                            : window.tr("Always allow · автоприменение", "Always allow · apply edits")
                                        color: inkSoft
                                        font.pixelSize: Math.round(9.5 * window.textScale) }
                                }
                                Rectangle { Layout.fillWidth: true
                                    height: 1
                                    color: themeTokens.rule }
                                ListView {
                                    id: transcript
                                    objectName: "chatTranscript"
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    clip: true
                                    spacing: 11 * window.densityScale
                                    model: bridge.events
                                    boundsBehavior: Flickable.StopAtBounds
                                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                                    delegate: Item {
                                        id: messageRow
                                        required property var modelData
                                        property bool fromUser: (modelData.data || {}).role === "user"
                                        property bool eventStep: modelData.kind === "step" || modelData.kind === "tool"
                                        width: transcript.width
                                        height: messageBubble.implicitHeight + 8
                                        Rectangle {
                                            id: messageBubble
                                            x: messageRow.fromUser ? messageRow.width - width : 0
                                            y: 4
                                            width: messageRow.fromUser
                                                ? Math.min(messageRow.width * 0.76, Math.max(180, bodyText.implicitWidth + 28))
                                                : messageRow.width * (messageRow.eventStep ? 0.76 : 0.82)
                                            implicitHeight: messageColumn.implicitHeight + 24
                                            radius: messageRow.eventStep ? 13 : 20
                                            color: modelData.kind === "error" ? "#12D36F83"
                                                 : messageRow.fromUser ? themeTokens.emberTint
                                                 : messageRow.eventStep ? themeTokens.surface : themeTokens.surface2
                                            border.color: modelData.kind === "error" ? themeTokens.fail
                                                        : messageRow.fromUser ? themeTokens.lineStrong : themeTokens.lineSoft
                                            ColumnLayout {
                                                id: messageColumn
                                                anchors.fill: parent
                                                anchors.margins: 13
                                                spacing: 4 * window.densityScale
                                                Text {
                                                    font.family: themeTokens.text
                                                    visible: messageRow.fromUser
                                                             || messageRow.eventStep
                                                             || modelData.kind === "error"
                                                    text: {
                                                        const data = modelData.data || {}
                                                        if (messageRow.fromUser)
                                                            return window.tr("Вы", "You")
                                                        if (modelData.kind === "step")
                                                            return String(data.node || window.tr("Этап", "Stage"))
                                                                + " · " + window.statusLabel(data.status)
                                                        if (modelData.kind === "tool")
                                                            return String(data.name || window.tr("Инструмент", "Tool"))
                                                        return window.tr("Ошибка", "Error")
                                                    }
                                                    color: modelData.kind === "error" ? themeTokens.fail
                                                         : messageRow.fromUser ? themeTokens.ember : ocean
                                                    font.pixelSize: Math.round(9.5 * window.textScale)
                                                    font.bold: true
                                                    Layout.fillWidth: true
                                                    elide: Text.ElideRight
                                                }
                                                Text {
                                                    id: bodyText
                                                    font.family: themeTokens.text
                                                    visible: !!modelData.text
                                                             && modelData.kind !== "snapshot"
                                                             && modelData.kind !== "step"
                                                    text: modelData.text
                                                    color: modelData.kind === "error" ? themeTokens.fail
                                                         : messageRow.fromUser ? ink : inkSoft
                                                    font.pixelSize: Math.round((messageRow.fromUser ? 11.5 : 10.5) * window.textScale)
                                                    font.bold: messageRow.fromUser
                                                    wrapMode: Text.Wrap
                                                    textFormat: Text.PlainText
                                                    Layout.fillWidth: true
                                                }
                                            }
                                        }
                                    }
                                    ColumnLayout {
                                        anchors.centerIn: parent
                                        width: Math.min(parent.width - 40, 820)
                                        spacing: (window.width < 1320 ? 8 : 12) * window.densityScale
                                        visible: bridge.events.length === 0

                                        RowLayout {
                                            objectName: "chatWelcomeHero"
                                            Layout.fillWidth: true
                                            spacing: (window.width < 1320 ? 14 : 22) * window.densityScale
                                            Rectangle {
                                                Layout.preferredWidth: window.width < 1320
                                                    ? 84 * window.densityScale : 132 * window.densityScale
                                                Layout.preferredHeight: Layout.preferredWidth
                                                radius: width / 2
                                                color: themeTokens.surface2
                                                border.color: themeTokens.lineStrong
                                                gradient: Gradient {
                                                    GradientStop { position: 0.0; color: themeTokens.aquaTint }
                                                    GradientStop { position: 1.0; color: themeTokens.mintTint }
                                                }
                                                LivingCore {
                                                    anchors.fill: parent
                                                    anchors.margins: 5 * window.densityScale
                                                    energy: 0.2
                                                    accent: themeTokens.aqua
                                                    secondary: themeTokens.cobalt
                                                    active: bridge.motionEnabled
                                                    motionEnabled: bridge.motionEnabled
                                                }
                                            }
                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                spacing: 7 * window.densityScale
                                                RowLayout {
                                                    spacing: 6
                                                    Rectangle {
                                                        width: 6
                                                        height: 6
                                                        radius: 3
                                                        color: bridge.running ? themeTokens.aqua : themeTokens.mint
                                                    }
                                                    Text {
                                                        text: window.tr("GIGAFORGE · ГОТОВ К РАБОТЕ", "GIGAFORGE · READY TO WORK")
                                                        color: themeTokens.ink3
                                                        font.family: themeTokens.mono
                                                        font.pixelSize: Math.round(8 * window.textScale)
                                                        font.letterSpacing: 0.9
                                                    }
                                                }
                                                Text {
                                                    text: bridge.mode === "chat"
                                                        ? window.tr("Чем займёмся?", "What can we work on?")
                                                        : window.tr("Начните с одной задачи", "Start with one task")
                                                    color: ink
                                                    font.family: themeTokens.display
                                                    font.pixelSize: Math.round((window.width < 1320 ? 24 : 29) * window.textScale)
                                                    font.bold: true
                                                    Layout.fillWidth: true
                                                    wrapMode: Text.WordWrap
                                                }
                                                Text {
                                                    text: bridge.mode === "chat"
                                                        ? window.tr("Идеи, проект и исследования — в одном живом рабочем пространстве.",
                                                                    "Ideas, project work and research — together in one live workspace.")
                                                        : bridge.mode === "agent"
                                                            ? window.tr("Опишите задачу для одного цикла работы и проверки.",
                                                                        "Describe a task for one focused cycle of work and verification.")
                                                            : window.tr("Задайте измеримый результат — Goal будет проверять каждый цикл.",
                                                                        "Define an outcome for Goal to verify after each cycle.")
                                                    color: inkSoft
                                                    font.family: themeTokens.text
                                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                                    Layout.fillWidth: true
                                                    Layout.maximumWidth: 560
                                                    wrapMode: Text.WordWrap
                                                }
                                                RowLayout {
                                                    Layout.topMargin: 2 * window.densityScale
                                                    spacing: 6 * window.densityScale
                                                    Rectangle {
                                                        Layout.preferredWidth: 22
                                                        Layout.preferredHeight: 22
                                                        radius: 11
                                                        color: themeTokens.aquaTint
                                                        Text {
                                                            anchors.centerIn: parent
                                                            text: "▣"
                                                            color: themeTokens.aquaInk
                                                            font.pixelSize: 10
                                                        }
                                                    }
                                                    Text {
                                                        text: bridge.projectName
                                                        color: themeTokens.ink
                                                        font.family: themeTokens.text
                                                        font.pixelSize: Math.round(9.5 * window.textScale)
                                                        font.bold: true
                                                        elide: Text.ElideRight
                                                    }
                                                    Text {
                                                        text: "· " + bridge.files.length + window.tr(" файлов в контексте", " project files in context")
                                                        color: themeTokens.ink3
                                                        font.family: themeTokens.text
                                                        font.pixelSize: Math.round(8.5 * window.textScale)
                                                        elide: Text.ElideRight
                                                        Layout.fillWidth: true
                                                    }
                                                    Button {
                                                        objectName: "welcomeSwitchProjectButton"
                                                        text: window.tr("Сменить", "Switch")
                                                        Accessible.name: window.tr("Сменить текущий проект", "Switch the current project")
                                                        onClicked: projectFolderDialog.open()
                                                        background: Rectangle {
                                                            radius: themeTokens.radiusPill
                                                            color: parent.hovered ? themeTokens.mintTint : "transparent"
                                                        }
                                                        contentItem: Text {
                                                            text: parent.text
                                                            color: themeTokens.primaryDeep
                                                            font.family: themeTokens.text
                                                            font.pixelSize: Math.round(8.5 * window.textScale)
                                                            font.bold: true
                                                            horizontalAlignment: Text.AlignHCenter
                                                            verticalAlignment: Text.AlignVCenter
                                                            leftPadding: 8
                                                            rightPadding: 8
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        GridLayout {
                                            objectName: "forgeStarterGrid"
                                            Layout.fillWidth: true
                                            columns: width < 560 ? 1 : 2
                                            columnSpacing: 10 * window.densityScale
                                            rowSpacing: (window.width < 1320 ? 6 : 10) * window.densityScale
                                            Repeater {
                                                objectName: "forgeStarterRepeater"
                                                model: window.forgeStarters
                                                delegate: Button {
                                                    required property int index
                                                    required property var modelData
                                                    objectName: "forgeStarterButton_" + index
                                                    Layout.fillWidth: true
                                                    Layout.preferredHeight: (window.width < 1320 ? 78 : 96) * window.densityScale
                                                    padding: 0
                                                    Accessible.name: modelData.title + ". " + modelData.detail
                                                    onClicked: {
                                                        composer.text = modelData.prompt
                                                        composer.forceActiveFocus()
                                                    }

                                                    background: Rectangle {
                                                        radius: themeTokens.radiusControl
                                                        color: parent.down ? themeTokens.emberTint
                                                             : parent.hovered
                                                                 ? Qt.rgba(modelData.tint.r, modelData.tint.g, modelData.tint.b, 0.34)
                                                                 : themeTokens.surface2
                                                        border.color: parent.activeFocus ? ocean
                                                                     : parent.down ? themeTokens.ember
                                                                     : parent.hovered ? themeTokens.lineStrong : themeTokens.lineSoft
                                                        border.width: parent.activeFocus ? 2 : 1
                                                        Behavior on color { ColorAnimation { duration: bridge.motionEnabled ? 140 : 0 } }
                                                        Behavior on border.color { ColorAnimation { duration: bridge.motionEnabled ? 140 : 0 } }
                                                    }

                                                    contentItem: RowLayout {
                                                        anchors.fill: parent
                                                        anchors.margins: 13 * window.densityScale
                                                        spacing: 11 * window.densityScale
                                                        Rectangle {
                                                            Layout.preferredWidth: 34 * window.densityScale
                                                            Layout.preferredHeight: 34 * window.densityScale
                                                            radius: themeTokens.radius
                                                            color: modelData.tint
                                                            Item {
                                                                anchors.centerIn: parent
                                                                width: 20
                                                                height: 20
                                                                visible: modelData.icon === "inspect"
                                                                Rectangle {
                                                                    x: 2
                                                                    y: 2
                                                                    width: 11
                                                                    height: 11
                                                                    radius: 6
                                                                    color: "transparent"
                                                                    border.color: ocean
                                                                    border.width: 2
                                                                }
                                                                Rectangle {
                                                                    x: 12
                                                                    y: 12
                                                                    width: 6
                                                                    height: 2
                                                                    radius: 1
                                                                    rotation: 45
                                                                    color: ocean
                                                                }
                                                            }
                                                            Item {
                                                                anchors.centerIn: parent
                                                                width: 20
                                                                height: 20
                                                                visible: modelData.icon === "create"
                                                                Rectangle {
                                                                    anchors.centerIn: parent
                                                                    width: 2
                                                                    height: 13
                                                                    radius: 1
                                                                    color: ocean
                                                                }
                                                                Rectangle {
                                                                    anchors.centerIn: parent
                                                                    width: 13
                                                                    height: 2
                                                                    radius: 1
                                                                    color: ocean
                                                                }
                                                            }
                                                            Item {
                                                                anchors.centerIn: parent
                                                                width: 20
                                                                height: 20
                                                                visible: modelData.icon === "quality"
                                                                Rectangle {
                                                                    x: 2
                                                                    y: 9
                                                                    width: 7
                                                                    height: 2
                                                                    radius: 1
                                                                    rotation: 45
                                                                    color: ocean
                                                                }
                                                                Rectangle {
                                                                    x: 6
                                                                    y: 9
                                                                    width: 12
                                                                    height: 2
                                                                    radius: 1
                                                                    rotation: -45
                                                                    color: ocean
                                                                }
                                                            }
                                                        }
                                                        ColumnLayout {
                                                            Layout.fillWidth: true
                                                            spacing: 4 * window.densityScale
                                                            Text {
                                                                text: modelData.title
                                                                color: ink
                                                                font.family: themeTokens.text
                                                                font.pixelSize: Math.round(12 * window.textScale)
                                                                font.bold: true
                                                            }
                                                            Text {
                                                                text: modelData.detail
                                                                color: inkSoft
                                                                font.family: themeTokens.text
                                                                font.pixelSize: Math.round(10 * window.textScale)
                                                                wrapMode: Text.WordWrap
                                                                elide: Text.ElideRight
                                                                Layout.fillWidth: true
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    onCountChanged: Qt.callLater(positionViewAtEnd)
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 10 * window.densityScale
                                    Rectangle {
                                        Layout.fillWidth: true
                                        height: 2
                                        color: themeTokens.recess
                                        Rectangle {
                                            objectName: "forgeProgressFill"
                                            width: parent.width * window.displayProgressPercent / 100
                                            height: 2
                                            color: themeTokens.ember
                                            Behavior on width { NumberAnimation { duration: bridge.motionEnabled ? 250 : 0 } }
                                        }
                                    }
                                    Text {
                                        text: window.displayProgress
                                        color: themeTokens.mist
                                        font.family: themeTokens.mono
                                        font.pixelSize: Math.round(9.5 * window.textScale)
                                        Layout.preferredWidth: 90
                                        horizontalAlignment: Text.AlignRight
                                    }
                                }
                            }
                        }
                    }

                    Item {
                        GlassCard {
                            anchors.fill: parent
                            inset: 0
                            ColumnLayout {
                                anchors.fill: parent
                                spacing: 0 * window.densityScale

                                RowLayout {
                                    Layout.fillWidth: true
                                    Layout.margins: 16
                                    Text {
                                        text:window.tr("Роли", "Roles")
                                        color: themeTokens.mist
                                        font.family: themeTokens.display
                                        font.pixelSize: Math.round(10 * window.textScale)
                                        font.bold: true
                                        font.letterSpacing: 1.6
                                        font.capitalization: Font.AllUppercase
                                    }
                                    Item { Layout.fillWidth: true }
                                    Text {
                                        text: window.tr("имя · назначение · состояние", "name · role · status")
                                        color: themeTokens.mist
                                        font.family: themeTokens.mono
                                        font.pixelSize: Math.round(9 * window.textScale)
                                    }
                                    Text {
                                        text: bridge.agents.length
                                        color: themeTokens.graphite
                                        font.family: themeTokens.mono
                                        font.pixelSize: Math.round(9.5 * window.textScale)
                                        Layout.leftMargin: 10 * window.densityScale
                                    }
                                }
                                Rectangle {
                                    Layout.fillWidth: true
                                    height: 1
                                    color: themeTokens.rule
                                }

                                ListView {
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    clip: true
                                    spacing: 0 * window.densityScale
                                    model: bridge.agents
                                    delegate: Item {
                                        required property var modelData
                                        width: ListView.view.width
                                        height: 38

                                        Rectangle {
                                            anchors.fill: parent
                                            color: roleHover.hovered ? themeTokens.paper : "transparent"
                                        }
                                        Rectangle {
                                            anchors.left: parent.left
                                            anchors.right: parent.right
                                            anchors.bottom: parent.bottom
                                            height: 1
                                            color: themeTokens.rule
                                        }
                                        HoverHandler { id: roleHover }

                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 16
                                            anchors.rightMargin: 16
                                            spacing: 10 * window.densityScale

                                            Rectangle {
                                                width: 18
                                                height: 18
                                                radius: 1
                                                color: "transparent"
                                                border.color: themeTokens.ruleStrong
                                                border.width: 1
                                                Text {
                                                    anchors.centerIn: parent
                                                    text: String(modelData.name || "?").slice(0, 1).toUpperCase()
                                                    color: themeTokens.graphite
                                                    font.family: themeTokens.display
                                                    font.pixelSize: Math.round(9 * window.textScale)
                                                    font.bold: true
                                                }
                                            }
                                            Text {
                                                text: modelData.name
                                                color: themeTokens.ink
                                                font.pixelSize: Math.round(12 * window.textScale)
                                                Layout.preferredWidth: 128
                                                elide: Text.ElideRight
                                            }
                                            Text {
                                                text: window.agentRoleLabel(modelData)
                                                color: themeTokens.graphite
                                                font.pixelSize: Math.round(10.5 * window.textScale)
                                                elide: Text.ElideRight
                                                Layout.fillWidth: true
                                            }
                                            Text {
                                                text: window.agentStatusLabel(modelData.status)
                                                color: modelData.statusColor
                                                font.family: themeTokens.mono
                                                font.pixelSize: Math.round(9.5 * window.textScale)
                                                Layout.preferredWidth: 96
                                                horizontalAlignment: Text.AlignRight
                                                elide: Text.ElideRight
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Item {
                        id: monitoringPage
                        objectName: "monitoringWorkspace"
                        property string eventFilter: "all"
                        readonly property var runtimeEvents: {
                            const events = bridge.events
                            let latestCommand = -1
                            for (let index = 0; index < events.length; index++) {
                                const event = events[index]
                                if (event.kind === "command" && ((event.data || {}).role === "user"))
                                    latestCommand = index
                            }
                            return events.slice(latestCommand + 1).filter(function(event) {
                                return event.kind === "step" || event.kind === "tool" || event.kind === "error"
                            })
                        }
                        readonly property int currentErrorCount: monitoringPage.runtimeEvents.filter(function(event) {
                            return event.kind === "error"
                        }).length
                        readonly property int toolCallCount: monitoringPage.runtimeEvents.filter(function(event) {
                            return event.kind === "tool"
                        }).length
                        readonly property var activeAgents: bridge.agents.filter(function(agent) {
                            const status = String(agent.status || "")
                            return status !== "В каталоге" && status !== "Available"
                        })
                        readonly property var filteredEvents: monitoringPage.runtimeEvents.slice().reverse().filter(function(event) {
                            if (monitoringPage.eventFilter === "errors")
                                return event.kind === "error"
                            if (monitoringPage.eventFilter === "tools")
                                return event.kind === "tool"
                            return true
                        })
                        readonly property real measuredNodeTimeMs: bridge.lastSteps.reduce(function(total, step) {
                            return total + Math.max(0, Number(step.ms) || 0)
                        }, 0)
                        function durationLabel(value) {
                            const seconds = Math.round(Math.max(0, Number(value) || 0) / 1000)
                            if (!seconds)
                                return "—"
                            const remainder = seconds % 60
                            return Math.floor(seconds / 60) + ":" + (remainder < 10 ? "0" : "") + remainder
                        }
                        GlassCard {
                            anchors.fill: parent
                            inset: 0
                            ColumnLayout {
                                anchors.fill: parent
                                spacing: 0 * window.densityScale

                                RowLayout {
                                    Layout.fillWidth: true
                                    Layout.margins: 16
                                    Text {
                                        text:window.tr("СОСТОЯНИЕ СИСТЕМЫ", "SYSTEM HEALTH")
                                        color: themeTokens.mist
                                        font.family: themeTokens.display
                                        font.pixelSize: Math.round(10 * window.textScale)
                                        font.bold: true
                                        font.letterSpacing: 1.6
                                        font.capitalization: Font.AllUppercase
                                    }
                                    Item { Layout.fillWidth: true }
                                    Text {
                                        text: bridge.connectionStatus
                                        color: themeTokens.graphite
                                        font.family: themeTokens.mono
                                        font.pixelSize: Math.round(9.5 * window.textScale)
                                    }
                                }
                                Rectangle { Layout.fillWidth: true; height: 1; color: themeTokens.rule }

                                RowLayout {
                                    objectName: "monitoringResources"
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 78
                                    Layout.leftMargin: 16
                                    Layout.rightMargin: 16
                                    Repeater {
                                        model: [
                                            {
                                                label: window.tr("ТОКЕНЫ · ПОСЛЕДНИЙ ЗАПУСК", "TOKENS · LAST RUN"),
                                                value: bridge.runTokenTotal > 0
                                                    ? bridge.runTokenTotal.toLocaleString()
                                                    : "—",
                                                note: bridge.taskTokenBudget > 0
                                                    ? window.tr("лимит ", "limit ") + bridge.taskTokenBudget.toLocaleString()
                                                    : window.tr("последний сохранённый запуск", "latest recorded run")
                                            },
                                            {
                                                label: window.tr("СУММА ДЛИТЕЛЬНОСТЕЙ УЗЛОВ", "NODE DURATION SUM"),
                                                value: monitoringPage.durationLabel(monitoringPage.measuredNodeTimeMs),
                                                note: window.tr("сумма фактических замеров", "sum of measured node times")
                                            },
                                            {
                                                label: window.tr("ВЫЗОВЫ ИНСТРУМЕНТОВ", "TOOL CALLS"),
                                                value: String(monitoringPage.toolCallCount),
                                                note: window.tr("в текущей истории", "in current history")
                                            }
                                        ]
                                        delegate: Item {
                                            required property var modelData
                                            required property int index
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true
                                            Rectangle {
                                                anchors.left: parent.left
                                                anchors.top: parent.top
                                                anchors.bottom: parent.bottom
                                                width: 1
                                                color: themeTokens.rule
                                                visible: index > 0
                                            }
                                            ColumnLayout {
                                                anchors.fill: parent
                                                anchors.margins: 12
                                                spacing: 3
                                                Text {
                                                    text: modelData.label
                                                    color: themeTokens.mist
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(8.5 * window.textScale)
                                                }
                                                Text {
                                                    text: modelData.value
                                                    color: themeTokens.ink
                                                    font.family: themeTokens.display
                                                    font.pixelSize: Math.round(18 * window.textScale)
                                                    font.bold: true
                                                }
                                                Text {
                                                    text: modelData.note
                                                    color: themeTokens.ink3
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(8.5 * window.textScale)
                                                    elide: Text.ElideRight
                                                    Layout.fillWidth: true
                                                }
                                            }
                                        }
                                    }
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 92
                                    Layout.maximumHeight: 92
                                    spacing: 0 * window.densityScale
                                    Repeater {
                                        model: [
                                            {
                                                label: window.tr("ПОДКЛЮЧЕНИЕ", "CONNECTION"),
                                                value: bridge.connected ? window.tr("Онлайн", "Online") : window.tr("Офлайн", "Offline"),
                                                note: bridge.connectionStatus
                                            },
                                            {
                                                label: window.tr("ПОСЛЕДНИЙ ЗАПУСК", "LATEST RUN"),
                                                value: window.displayRunStatus,
                                                note: bridge.running ? window.tr("выполняется сейчас", "currently running") : window.tr("текущее состояние", "current status")
                                            },
                                            {
                                                label: window.tr("УЧАСТНИКИ ЗАПУСКА", "RUN PARTICIPANTS"),
                                                value: String(monitoringPage.activeAgents.length),
                                                note: window.tr("сообщены runtime", "reported by runtime")
                                            },
                                            {
                                                label: window.tr("ОШИБКИ", "ERRORS"),
                                                value: String(monitoringPage.currentErrorCount),
                                                note: window.tr("записаны в событиях", "recorded in events")
                                            }
                                        ]
                                        delegate: Item {
                                            required property var modelData
                                            required property int index
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true
                                            Rectangle {
                                                anchors.left: parent.left
                                                anchors.top: parent.top
                                                anchors.bottom: parent.bottom
                                                width: 1
                                                color: themeTokens.rule
                                                visible: index > 0
                                            }
                                            ColumnLayout {
                                                anchors.fill: parent
                                                anchors.margins: 16
                                                spacing: 4 * window.densityScale
                                                Text {
                                                    text: modelData.label
                                                    color: themeTokens.mist
                                                    font.family: themeTokens.display
                                                    font.pixelSize: Math.round(9 * window.textScale)
                                                    font.bold: true
                                                    font.letterSpacing: 1.4
                                                    font.capitalization: Font.AllUppercase
                                                }
                                                Text {
                                                    text: modelData.value
                                                    color: themeTokens.ink
                                                    font.family: themeTokens.display
                                                    font.pixelSize: Math.round(22 * window.textScale)
                                                    font.bold: true
                                                    elide: Text.ElideRight
                                                    Layout.fillWidth: true
                                                }
                                                Text {
                                                    text: modelData.note
                                                    color: themeTokens.mist
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(9 * window.textScale)
                                                    elide: Text.ElideRight
                                                    Layout.fillWidth: true
                                                }
                                            }
                                        }
                                    }
                                }
                                Rectangle { Layout.fillWidth: true; height: 1; color: themeTokens.rule }

                                RowLayout {
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    spacing: 0 * window.densityScale

                                    ColumnLayout {
                                        Layout.preferredWidth: 250
                                        Layout.minimumWidth: 180
                                        Layout.fillHeight: true
                                        Layout.margins: 16
                                        spacing: 10 * window.densityScale
                                        RowLayout {
                                            Layout.fillWidth: true
                                            Text {
                                                text: window.tr("УЧАСТНИКИ ЗАПУСКА", "RUN PARTICIPANTS")
                                                color: themeTokens.mist
                                                font.family: themeTokens.display
                                                font.pixelSize: Math.round(9 * window.textScale)
                                                font.bold: true
                                                font.letterSpacing: 1.1
                                                font.capitalization: Font.AllUppercase
                                            }
                                            Item { Layout.fillWidth: true }
                                            Text {
                                                text: String(monitoringPage.activeAgents.length)
                                                color: themeTokens.ink3
                                                font.family: themeTokens.mono
                                                font.pixelSize: Math.round(9.5 * window.textScale)
                                            }
                                        }
                                        ListView {
                                            id: monitoringAgentsList
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true
                                            clip: true
                                            spacing: 4 * window.densityScale
                                            model: monitoringPage.activeAgents
                                            delegate: RowLayout {
                                                required property var modelData
                                                width: monitoringAgentsList.width
                                                height: 38
                                                spacing: 8 * window.densityScale
                                                Rectangle {
                                                    width: 7
                                                    height: 7
                                                    radius: 4
                                                    color: modelData.statusColor || themeTokens.mint
                                                }
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: 1
                                                    Text {
                                                        text: modelData.name || modelData.role || window.tr("Участник", "Participant")
                                                        color: themeTokens.ink
                                                        font.pixelSize: Math.round(10.5 * window.textScale)
                                                        font.bold: true
                                                        elide: Text.ElideRight
                                                        Layout.fillWidth: true
                                                    }
                                                    Text {
                                                        text: modelData.roleRu || modelData.role || ""
                                                        color: themeTokens.ink3
                                                        font.pixelSize: Math.round(9 * window.textScale)
                                                        elide: Text.ElideRight
                                                        Layout.fillWidth: true
                                                    }
                                                }
                                                Text {
                                                    text: modelData.status || ""
                                                    color: modelData.statusColor || themeTokens.ink3
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(8.5 * window.textScale)
                                                    elide: Text.ElideRight
                                                    Layout.maximumWidth: 80
                                                }
                                            }
                                            Text {
                                                anchors.centerIn: parent
                                                visible: monitoringPage.activeAgents.length === 0
                                                text: window.tr("Нет участников, сообщённых runtime.", "No participants reported by runtime.")
                                                color: themeTokens.ink3
                                                font.pixelSize: Math.round(10 * window.textScale)
                                                wrapMode: Text.WordWrap
                                                horizontalAlignment: Text.AlignHCenter
                                                width: parent.width - 20
                                            }
                                        }
                                    }

                                    Rectangle { Layout.fillHeight: true; width: 1; color: themeTokens.rule }

                                    ColumnLayout {
                                        Layout.preferredWidth: 420
                                        Layout.fillHeight: true
                                        Layout.margins: 16
                                        spacing: 6 * window.densityScale
                                        RowLayout {
                                            Layout.fillWidth: true
                                            Text {
                                                text: window.tr("АКТИВНОСТЬ RUNTIME", "RUNTIME ACTIVITY")
                                                color: themeTokens.mist
                                                font.family: themeTokens.display
                                                font.pixelSize: Math.round(9 * window.textScale)
                                                font.bold: true
                                                font.letterSpacing: 1.4
                                                font.capitalization: Font.AllUppercase
                                            }
                                            Item { Layout.fillWidth: true }
                                            Text {
                                                text: String(monitoringPage.filteredEvents.length)
                                                color: themeTokens.ink3
                                                font.family: themeTokens.mono
                                                font.pixelSize: Math.round(9.5 * window.textScale)
                                            }
                                        }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 5 * window.densityScale
                                            Repeater {
                                                model: [
                                                    { key: "all", ru: "Все", en: "All" },
                                                    { key: "errors", ru: "Ошибки", en: "Errors" },
                                                    { key: "tools", ru: "Инструменты", en: "Tools" }
                                                ]
                                                delegate: Button {
                                                    required property var modelData
                                                    text: window.language === "en" ? modelData.en : modelData.ru
                                                    onClicked: monitoringPage.eventFilter = modelData.key
                                                    background: Rectangle {
                                                        radius: themeTokens.radiusPill
                                                        color: monitoringPage.eventFilter === modelData.key ? themeTokens.primaryTint : themeTokens.surface2
                                                        border.color: monitoringPage.eventFilter === modelData.key ? themeTokens.primary : themeTokens.line
                                                    }
                                                    contentItem: Text {
                                                        text: parent.text
                                                        color: monitoringPage.eventFilter === modelData.key ? themeTokens.primaryDeep : themeTokens.ink3
                                                        font.family: themeTokens.text
                                                        font.pixelSize: Math.round(9 * window.textScale)
                                                        horizontalAlignment: Text.AlignHCenter
                                                        verticalAlignment: Text.AlignVCenter
                                                        leftPadding: 8
                                                        rightPadding: 8
                                                    }
                                                    Layout.preferredHeight: 24
                                                }
                                            }
                                            Item { Layout.fillWidth: true }
                                        }
                                        Item {
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true
                                            ListView {
                                                anchors.fill: parent
                                                visible: monitoringPage.filteredEvents.length > 0
                                                clip: true
                                                spacing: 0 * window.densityScale
                                                model: monitoringPage.filteredEvents
                                                delegate: Item {
                                                    required property var modelData
                                                    width: ListView.view.width
                                                    height: 30
                                                    Rectangle {
                                                        anchors.left: parent.left
                                                        anchors.right: parent.right
                                                        anchors.bottom: parent.bottom
                                                        height: 1
                                                        color: themeTokens.rule
                                                    }
                                                    RowLayout {
                                                        anchors.fill: parent
                                                        spacing: 8 * window.densityScale
                                                        Text {
                                                            text: modelData.kind === "error" ? "ошибка"
                                                                : modelData.kind === "step" ? "этап"
                                                                : modelData.kind === "tool" ? "инстр."
                                                                : modelData.kind === "command" ? "задача" : "событие"
                                                            color: modelData.kind === "error" ? themeTokens.fail : themeTokens.mist
                                                            font.family: themeTokens.mono
                                                            font.pixelSize: Math.round(9 * window.textScale)
                                                            Layout.preferredWidth: 44
                                                        }
                                                        Text {
                                                            text: ((modelData.data || {}).summary || modelData.text || ((modelData.data || {}).status || "обновление состояния"))
                                                            color: modelData.kind === "error" ? themeTokens.fail : themeTokens.graphite
                                                            font.pixelSize: Math.round(10.5 * window.textScale)
                                                            elide: Text.ElideRight
                                                            Layout.fillWidth: true
                                                        }
                                                    }
                                                }
                                            }
                                            Text {
                                                anchors.centerIn: parent
                                                visible: monitoringPage.filteredEvents.length === 0
                                                text: monitoringPage.runtimeEvents.length === 0
                                                    ? window.tr("События появятся после запуска задачи.", "Events will appear after a task starts.")
                                                    : window.tr("Нет событий в этом фильтре.", "No events match this filter.")
                                                color: themeTokens.mist
                                                font.pixelSize: Math.round(10.5 * window.textScale)
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Item {
                        GlassCard {
                            anchors.fill: parent
                            inset: 0

                            RowLayout {
                                anchors.fill: parent
                                spacing: 0 * window.densityScale

                                ScrollView {
                                    id: settingsScroll
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    clip: true
                                    contentWidth: availableWidth
                                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                                    ColumnLayout {
                                        x: Math.max(20, (settingsScroll.availableWidth - width) / 2)
                                        width: Math.min(settingsScroll.availableWidth - 40, 640)
                                        spacing: 10 * window.densityScale

                                        Item { Layout.fillWidth: true; height: 16 }

                                        Text {
                                            text:window.tr("00 · Оформление", "00 · Appearance")
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9.5 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.6
                                            font.capitalization: Font.AllUppercase
                                        }
                                        Rectangle { Layout.fillWidth: true; height: 1; color: themeTokens.rule }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 6 * window.densityScale
                                            Repeater {
                                                model: [
                                                    { key: "pearl", ru: "Жемчуг", en: "Pearl", swatchA: "#D5F5E8", swatchB: "#83CEC0" },
                                                    { key: "graphite", ru: "Графит", en: "Graphite", swatchA: "#28333A", swatchB: "#2DD4BF" },
                                                    { key: "verdant", ru: "Живая зелень", en: "Verdant", swatchA: "#0B2B22", swatchB: "#34D399" }
                                                ]
                                                delegate: Button {
                                                    objectName: "settingsThemeButton_" + modelData.key
                                                    required property var modelData
                                                    Layout.fillWidth: true
                                                    Layout.preferredHeight: 38
                                                    onClicked: bridge.setAppearanceTheme(modelData.key)
                                                    background: Rectangle {
                                                        radius: themeTokens.radius
                                                        color: bridge.appearanceTheme === modelData.key ? themeTokens.primaryTint : themeTokens.surface
                                                        border.color: bridge.appearanceTheme === modelData.key ? themeTokens.lineStrong : themeTokens.line
                                                    }
                                                    contentItem: RowLayout {
                                                        spacing: 7 * window.densityScale
                                                        Rectangle {
                                                            Layout.preferredWidth: 17
                                                            Layout.preferredHeight: 17
                                                            radius: 9
                                                            border.color: themeTokens.lineStrong
                                                            border.width: 1
                                                            gradient: Gradient {
                                                                GradientStop { position: 0; color: modelData.swatchA }
                                                                GradientStop { position: 1; color: modelData.swatchB }
                                                            }
                                                        }
                                                        Text {
                                                            Layout.fillWidth: true
                                                            text: window.lang === "en" ? modelData.en : modelData.ru
                                                            color: bridge.appearanceTheme === modelData.key ? themeTokens.primaryDeep : themeTokens.ink2
                                                            font.family: themeTokens.text
                                                            font.pixelSize: Math.round(10 * window.textScale)
                                                            font.bold: bridge.appearanceTheme === modelData.key
                                                            horizontalAlignment: Text.AlignHCenter
                                                            verticalAlignment: Text.AlignVCenter
                                                        }
                                                        Text {
                                                            text: bridge.appearanceTheme === modelData.key ? "✓" : ""
                                                            color: themeTokens.primaryDeep
                                                            font.pixelSize: Math.round(10 * window.textScale)
                                                            font.bold: true
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 12 * window.densityScale
                                            Text { text:window.tr("Плотность", "Density"); color: themeTokens.ink; font.family: themeTokens.text; font.pixelSize: Math.round(10.5 * window.textScale); Layout.fillWidth: true }
                                            Repeater {
                                                model: [
                                                    { key: "airy", ru: "Воздушная", en: "Airy" },
                                                    { key: "compact", ru: "Компактная", en: "Compact" }
                                                ]
                                                delegate: Button {
                                                    required property var modelData
                                                    onClicked: bridge.setDensity(modelData.key)
                                                    background: Rectangle { radius: themeTokens.radiusPill; color: bridge.density === modelData.key ? themeTokens.primaryTint : "transparent"; border.color: bridge.density === modelData.key ? themeTokens.lineStrong : themeTokens.line }
                                                    contentItem: Text { text: window.lang === "en" ? modelData.en : modelData.ru; color: bridge.density === modelData.key ? themeTokens.primaryDeep : themeTokens.ink3; font.family: themeTokens.text; font.pixelSize: Math.round(9.5 * window.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 9; rightPadding: 9 }
                                                }
                                            }
                                        }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            Text { text:window.tr("Плавные переходы", "Motion effects"); color: themeTokens.ink; font.family: themeTokens.text; font.pixelSize: Math.round(10.5 * window.textScale); Layout.fillWidth: true }
                                            ThemedSwitch {
                                                checked: bridge.motionEnabled
                                                text: ""
                                                Accessible.name: window.tr("Плавные переходы", "Motion effects")
                                                accent: themeTokens.primary
                                                trackColor: themeTokens.surface3
                                                trackBorder: themeTokens.line
                                                labelColor: themeTokens.ink3
                                                activeLabelColor: themeTokens.ink
                                                onToggled: bridge.setMotionEnabled(checked)
                                            }
                                        }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 12 * window.densityScale
                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                spacing: 2 * window.densityScale
                                                Text {
                                                    text: window.tr("Адаптивная рабочая среда", "Adaptive workspace")
                                                    color: themeTokens.ink
                                                    font.family: themeTokens.text
                                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                                    Layout.fillWidth: true
                                                }
                                                Text {
                                                    text: window.tr(
                                                        "Меняет только видимость панелей; разрешения и режим агента не трогает.",
                                                        "Changes panel visibility only; agent mode and permissions are untouched."
                                                    )
                                                    color: themeTokens.ink3
                                                    font.family: themeTokens.text
                                                    font.pixelSize: Math.round(9.5 * window.textScale)
                                                    wrapMode: Text.WordWrap
                                                    Layout.fillWidth: true
                                                }
                                            }
                                            ThemedSwitch {
                                                objectName: "adaptiveWorkspaceSwitch"
                                                checked: bridge.adaptiveWorkspaceEnabled
                                                text: ""
                                                Accessible.name: window.tr("Адаптивная рабочая среда", "Adaptive workspace")
                                                accent: themeTokens.primary
                                                trackColor: themeTokens.surface3
                                                trackBorder: themeTokens.line
                                                labelColor: themeTokens.ink3
                                                activeLabelColor: themeTokens.ink
                                                onToggled: {
                                                    bridge.setAdaptiveWorkspaceEnabled(checked)
                                                    if (checked && window.adaptiveWorkspaceIntent !== "general")
                                                        window.applyAdaptiveWorkspace(window.adaptiveWorkspaceIntent)
                                                    else if (!checked)
                                                        window.restoreAdaptiveWorkspace()
                                                }
                                            }
                                        }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 12 * window.densityScale
                                            Text {
                                                text: window.tr("Переставляйте панели обзора за маркер в их углу.", "Drag dashboard panels by the handle in each corner.")
                                                color: themeTokens.ink3
                                                font.family: themeTokens.text
                                                font.pixelSize: Math.round(10 * window.textScale)
                                                wrapMode: Text.WordWrap
                                                Layout.fillWidth: true
                                            }
                                            Button {
                                                text: window.tr("Сбросить порядок", "Reset order")
                                                onClicked: bridge.resetDashboardPanelOrder()
                                                background: Rectangle { radius: themeTokens.radiusPill; color: themeTokens.surface; border.color: themeTokens.line }
                                                contentItem: Text { text: parent.text; color: themeTokens.ink2; font.family: themeTokens.text; font.pixelSize: Math.round(9.5 * window.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 10; rightPadding: 10 }
                                            }
                                        }

                                        Text {
                                            text:window.tr("01 · Подключение", "01 · Connection")
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9.5 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.6
                                            font.capitalization: Font.AllUppercase
                                        }
                                        Rectangle { Layout.fillWidth: true; height: 1; color: themeTokens.rule }

                                        Text {
                                            text: bridge.credentialConfigured
                                                  ? window.tr("Ключ сохранён в .env проекта. Введите новый, чтобы заменить.",
                                                              "A key is saved in the project .env. Enter a new one to replace it.")
                                                  : window.tr("Ключ ещё не сохранён: окно работает только на чтение.",
                                                              "No key is saved yet; the window is read-only.")
                                            color: themeTokens.graphite
                                            font.pixelSize: Math.round(11.5 * window.textScale)
                                            wrapMode: Text.WordWrap
                                            Layout.fillWidth: true
                                        }

                                        Text {
                                            text: window.tr("Новый ключ · base64(client_id:client_secret)",
                                                            "New key · base64(client_id:client_secret)")
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.3
                                            font.capitalization: Font.AllUppercase
                                        }
                                        TextField {
                                            id: credential
                                            objectName: "settingsCredentialField"
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 34
                                            placeholderText: bridge.credentialConfigured
                                                ? window.tr("Введите новый ключ для замены сохранённого", "Enter a new key to replace the saved one")
                                                : window.tr("Введите credential", "Enter credential")
                                            echoMode: TextInput.Password
                                            Layout.topMargin: 2 * window.densityScale
                                            selectByMouse: true
                                            color: themeTokens.ink
                                            font.family: themeTokens.mono
                                            font.pixelSize: Math.round(11.5 * window.textScale)
                                            leftPadding: 9
                                            rightPadding: 9
                                            topPadding: 6
                                            bottomPadding: 6
                                            background: Rectangle {
                                                radius: themeTokens.radiusControl
                                                color: themeTokens.surface
                                                border.color: parent.activeFocus ? themeTokens.primary : themeTokens.controlLine
                                                border.width: 1
                                            }
                                        }
                                        Text {
                                            text: "GigaChat API URL"
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.3
                                            font.capitalization: Font.AllUppercase
                                        }
                                        TextField {
                                            id: baseUrl
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 34
                                            placeholderText: ""
                                            text: bridge.credentialBaseUrl
                                            
                                            selectByMouse: true
                                            color: themeTokens.ink
                                            font.family: themeTokens.mono
                                            font.pixelSize: Math.round(11.5 * window.textScale)
                                            leftPadding: 9
                                            rightPadding: 9
                                            topPadding: 6
                                            bottomPadding: 6
                                            background: Rectangle {
                                                radius: themeTokens.radiusControl
                                                color: themeTokens.surface
                                                border.color: parent.activeFocus ? themeTokens.primary : themeTokens.controlLine
                                                border.width: 1
                                            }
                                        }
                                        Text {
                                            text: "OAuth URL"
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.3
                                            font.capitalization: Font.AllUppercase
                                        }
                                        TextField {
                                            id: oauthUrl
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 34
                                            placeholderText: ""
                                            text: bridge.credentialOauthUrl
                                            
                                            selectByMouse: true
                                            color: themeTokens.ink
                                            font.family: themeTokens.mono
                                            font.pixelSize: Math.round(11.5 * window.textScale)
                                            leftPadding: 9
                                            rightPadding: 9
                                            topPadding: 6
                                            bottomPadding: 6
                                            background: Rectangle {
                                                radius: themeTokens.radiusControl
                                                color: themeTokens.surface
                                                border.color: parent.activeFocus ? themeTokens.primary : themeTokens.controlLine
                                                border.width: 1
                                            }
                                        }
                                        Text {
                                            text: "Scope"
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.3
                                            font.capitalization: Font.AllUppercase
                                        }
                                        TextField {
                                            id: scope
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 34
                                            placeholderText: ""
                                            text: bridge.credentialScope
                                            
                                            selectByMouse: true
                                            color: themeTokens.ink
                                            font.family: themeTokens.mono
                                            font.pixelSize: Math.round(11.5 * window.textScale)
                                            leftPadding: 9
                                            rightPadding: 9
                                            topPadding: 6
                                            bottomPadding: 6
                                            background: Rectangle {
                                                radius: themeTokens.radiusControl
                                                color: themeTokens.surface
                                                border.color: parent.activeFocus ? themeTokens.primary : themeTokens.controlLine
                                                border.width: 1
                                            }
                                        }

                                        Item { Layout.fillWidth: true; height: 2 }

                                        CheckBox {
                                            id: tlsCheck
                                            objectName: "settingsTlsCheck"
                                            text:window.tr("Проверять TLS-сертификат", "Verify TLS certificate")
                                            checked: bridge.verifyTls
                                            Layout.fillWidth: true
                                            contentItem: Text {
                                                text: tlsCheck.text
                                                color: themeTokens.ink
                                                font.pixelSize: Math.round(11 * window.textScale)
                                                leftPadding: 22
                                                verticalAlignment: Text.AlignVCenter
                                            }
                                            indicator: Rectangle {
                                                x: 0
                                                y: parent.height / 2 - 8
                                                width: 16
                                                height: 16
                                                radius: 3
                                                color: tlsCheck.checked ? themeTokens.primary : themeTokens.surface
                                                border.color: tlsCheck.checked ? themeTokens.primary : themeTokens.controlLine
                                                border.width: 1
                                                Text {
                                                    anchors.centerIn: parent
                                                    visible: tlsCheck.checked
                                                    text: "✓"
                                                    color: themeTokens.plate
                                                    font.pixelSize: Math.round(9 * window.textScale)
                                                }
                                            }
                                        }
                                        Text {
                                            text: tlsCheck.checked
                                                  ? window.tr("Проверка включена. Отключите, если цепочка API нестабильна.",
                                                              "Verification is enabled. Disable it only if the API certificate chain is unstable.")
                                                  : window.tr("Проверка отключена для текущей нестабильной цепочки API.",
                                                              "Verification is disabled for the currently unstable API certificate chain.")
                                            color: themeTokens.mist
                                            font.pixelSize: Math.round(10.5 * window.textScale)
                                            wrapMode: Text.WordWrap
                                            Layout.fillWidth: true
                                        }

                                        RowLayout {
                                            Layout.fillWidth: true
                                            Layout.topMargin: 4 * window.densityScale
                                            spacing: 8 * window.densityScale
                                            Button {
                                                objectName: "settingsSaveCredentialsButton"
                                                text: bridge.credentialConfigured
                                                    ? window.tr("Сохранить и проверить", "Save and verify")
                                                    : window.tr("Подключить GigaChat", "Connect GigaChat")
                                                enabled: !bridge.running && !bridge.credentialBusy
                                                onClicked: bridge.saveCredentials(credential.text, baseUrl.text, oauthUrl.text, scope.text, tlsCheck.checked)
                                                Layout.preferredHeight: 30
                                                background: Rectangle {
                                                    radius: themeTokens.radius
                                                    color: parent.enabled ? (parent.pressed ? themeTokens.emberDeep : themeTokens.ember) : themeTokens.rule
                                                }
                                                contentItem: Text {
                                                    text: parent.text
                                                    color: themeTokens.actionInk
                                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                                    font.bold: true
                                                    horizontalAlignment: Text.AlignHCenter
                                                    verticalAlignment: Text.AlignVCenter
                                                    leftPadding: 14
                                                    rightPadding: 14
                                                }
                                            }
                                            Text {
                                                visible: bridge.credentialBusy
                                                text:window.tr("Проверяю подключение…", "Checking connection…")
                                                color: themeTokens.mist
                                                font.pixelSize: Math.round(10 * window.textScale)
                                                elide: Text.ElideRight
                                                Layout.fillWidth: true
                                            }
                                        }

                                        Item { Layout.fillWidth: true; height: 8 }

                                        Text {
                                            text:window.tr("02 · Правки файлов", "02 · File changes")
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9.5 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.6
                                            font.capitalization: Font.AllUppercase
                                        }
                                        Rectangle { Layout.fillWidth: true; height: 1; color: themeTokens.rule }

                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 6 * window.densityScale
                                            Repeater {
                                                model: [
                                                    { key: "ask", label: window.tr("Спрашивать", "Ask each time") },
                                                    { key: "always_allow", label: window.tr("Разрешить всегда", "Always allow") }
                                                ]
                                                delegate: Button {
                                                    required property var modelData
                                                    required property int index
                                                    objectName: "settingsApprovalButton_" + modelData.key
                                                    Layout.fillWidth: true
                                                    Layout.preferredHeight: 30
                                                    text: modelData.label
                                                    enabled: !bridge.running
                                                    onClicked: bridge.setApprovalMode(modelData.key)
                                                    background: Rectangle {
                                                        radius: themeTokens.radiusControl
                                                        color: bridge.approvalMode === modelData.key ? themeTokens.primaryTint : themeTokens.surface
                                                        border.color: bridge.approvalMode === modelData.key ? themeTokens.lineStrong : themeTokens.controlLine
                                                        border.width: 1
                                                    }
                                                    contentItem: Text {
                                                        text: parent.text
                                                        color: bridge.approvalMode === modelData.key ? themeTokens.primaryDeep : themeTokens.ink2
                                                        font.pixelSize: Math.round(10.5 * window.textScale)
                                                        font.bold: bridge.approvalMode === modelData.key
                                                        horizontalAlignment: Text.AlignHCenter
                                                        verticalAlignment: Text.AlignVCenter
                                                    }
                                                }
                                            }
                                        }
                                        Text {
                                            text: window.tr(
                                                "«Спрашивать» — один запрос на полный дифф перед записью. «Разрешить всегда» — запись без подтверждения; откат доступен в Diff.",
                                                "Ask each time requests approval for the full diff before writing. Always allow writes without confirmation; changes can still be reverted in Diff."
                                            )
                                            color: themeTokens.mist
                                            font.pixelSize: Math.round(10.5 * window.textScale)
                                            wrapMode: Text.WordWrap
                                            Layout.fillWidth: true
                                        }

                                        Item { Layout.fillWidth: true; height: 8 }

                                        Text {
                                            text:window.tr("03 · Цель", "03 · Goal")
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9.5 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.6
                                            font.capitalization: Font.AllUppercase
                                        }
                                        Rectangle { Layout.fillWidth: true; height: 1; color: themeTokens.rule }

                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 10 * window.densityScale
                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                spacing: 2 * window.densityScale
                                                Text {
                                                    text: window.goalRuntimeStatusLabel(bridge.goalRuntimeStatus)
                                                    color: bridge.goalRuntimeReady ? themeTokens.verified : bridge.goalRuntimeRunning ? themeTokens.graphite : themeTokens.fail
                                                    font.pixelSize: Math.round(11 * window.textScale)
                                                    wrapMode: Text.WordWrap
                                                    Layout.fillWidth: true
                                                }
                                                Text {
                                                    text:window.tr("Цикл: автор → проверка → правка. Незавершённая цель продолжается из «Истории».", "Cycle: author → check → fix. An unfinished goal continues from History.")
                                                    color: themeTokens.mist
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    wrapMode: Text.WordWrap
                                                    Layout.fillWidth: true
                                                }
                                            }
                                            BusyIndicator {
                                                visible: bridge.goalRuntimeRunning
                                                running: visible
                                                Layout.preferredWidth: 20
                                                Layout.preferredHeight: 20
                                            }
                                            Button {
                                                visible: !bridge.goalRuntimeReady && !bridge.goalRuntimeRunning
                                                text:window.tr("Повторить проверку", "Re-run check")
                                                onClicked: bridge.startGoalRuntimeSetup()
                                                Layout.preferredHeight: 28
                                                background: Rectangle {
                                                    radius: themeTokens.radius
                                                    color: themeTokens.plate
                                                    border.color: parent.activeFocus ? themeTokens.ember : themeTokens.ruleStrong
                                                    border.width: 1
                                                }
                                                contentItem: Text {
                                                    text: parent.text
                                                    color: themeTokens.ink
                                                    font.pixelSize: Math.round(10 * window.textScale)
                                                    horizontalAlignment: Text.AlignHCenter
                                                    verticalAlignment: Text.AlignVCenter
                                                    leftPadding: 10
                                                    rightPadding: 10
                                                }
                                            }
                                        }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 10 * window.densityScale
                                            Text {
                                                text:window.tr("Циклов Goal за один запуск", "Goal cycles per run")
                                                color: themeTokens.ink
                                                font.pixelSize: Math.round(11 * window.textScale)
                                                Layout.fillWidth: true
                                            }
                                            SpinBox {
                                                objectName: "settingsGoalCycles"
                                                from: 1
                                                to: 20
                                                value: bridge.goalMaxCycles
                                                editable: true
                                                enabled: !bridge.running
                                                Layout.preferredWidth: 96
                                                onValueModified: bridge.setGoalMaxCycles(value)
                                            }
                                        }
                                        Item { Layout.fillWidth: true; height: 16 }
                                    }
                                }

                                Rectangle {
                                    Layout.preferredWidth: 1
                                    Layout.fillHeight: true
                                    color: themeTokens.rule
                                }

                                Rectangle {
                                    Layout.preferredWidth: 328
                                    Layout.fillHeight: true
                                    color: themeTokens.paper

                                    ColumnLayout {
                                        anchors.fill: parent
                                        anchors.margins: 16
                                        spacing: 10 * window.densityScale

                                        Text {
                                            text:window.tr("Состояние", "Status")
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9.5 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.6
                                            font.capitalization: Font.AllUppercase
                                        }
                                        Rectangle { Layout.fillWidth: true; height: 1; color: themeTokens.rule }

                                        Repeater {
                                            model: [
                                                { k: window.tr("Ключ", "Key"),
                                                  v: bridge.credentialConfigured ? window.tr("сохранён", "saved") : window.tr("не задан", "not set"),
                                                  c: bridge.credentialConfigured ? themeTokens.verified : themeTokens.fail },
                                                { k: window.tr("Проверка TLS", "TLS verification"),
                                                  v: bridge.verifyTls ? window.tr("включена", "on") : window.tr("выключена", "off"),
                                                  c: bridge.verifyTls ? themeTokens.verified : themeTokens.warn },
                                                { k: window.tr("Модель", "Model"), v: bridge.modelName, c: themeTokens.ink },
                                                { k: window.tr("Файлов", "Files"), v: String(bridge.files.length), c: themeTokens.ink },
                                                { k: window.tr("Правки", "File changes"),
                                                  v: bridge.approvalMode === "ask" ? window.tr("спрашивать", "ask") : window.tr("разрешать", "allow"),
                                                  c: themeTokens.ink },
                                                { k: window.tr("Среда Goal", "Goal environment"),
                                                  v: bridge.goalRuntimeRunning ? window.tr("готовится", "setting up")
                                                      : bridge.goalRuntimeReady ? window.tr("готова", "ready") : window.tr("не готова", "not ready"),
                                                  c: bridge.goalRuntimeReady ? themeTokens.verified : bridge.goalRuntimeRunning ? themeTokens.graphite : themeTokens.warn }
                                            ]
                                            delegate: Item {
                                                required property var modelData
                                                Layout.fillWidth: true
                                                Layout.preferredHeight: 22
                                                Layout.maximumHeight: 22
                                                Rectangle {
                                                    anchors.left: parent.left
                                                    anchors.right: parent.right
                                                    anchors.bottom: parent.bottom
                                                    height: 1
                                                    color: themeTokens.rule
                                                }
                                                RowLayout {
                                                    anchors.fill: parent
                                                    spacing: 8 * window.densityScale
                                                    Text {
                                                        text: modelData.k
                                                        color: themeTokens.mist
                                                        font.pixelSize: Math.round(10.5 * window.textScale)
                                                    }
                                                    Item { Layout.fillWidth: true }
                                                    Text {
                                                        text: modelData.v
                                                        color: modelData.c
                                                        font.family: themeTokens.mono
                                                        font.pixelSize: Math.round(10 * window.textScale)
                                                        elide: Text.ElideMiddle
                                                        Layout.maximumWidth: 160
                                                    }
                                                }
                                            }
                                        }

                                        Item { Layout.fillWidth: true; height: 6 }

                                        Text {
                                            text:window.tr("Где хранится", "Where it is stored")
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9.5 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.6
                                            font.capitalization: Font.AllUppercase
                                        }
                                        Text {
                                            text: window.tr(
                                                "Ключ и адреса — в .env проекта. Без ключа окно работает только на чтение: телеметрия, индекс и команды доступны, запуск — нет.",
                                                "The key and URLs are stored in the project .env. Without a key, the window is read-only: telemetry, the index and commands remain available, but runs cannot start."
                                            )
                                            color: themeTokens.graphite
                                            font.pixelSize: Math.round(11 * window.textScale)
                                            lineHeight: 1.5
                                            wrapMode: Text.WordWrap
                                            Layout.fillWidth: true
                                        }
                                        Text {
                                            text:window.tr("В трассы, логи и диффы попадает только имя переменной.", "Only the variable name reaches traces, logs and diffs.")
                                            color: themeTokens.mist
                                            font.pixelSize: Math.round(10 * window.textScale)
                                            wrapMode: Text.WordWrap
                                            Layout.fillWidth: true
                                        }

                                        Item { Layout.fillWidth: true; Layout.fillHeight: true }

                                        Text {
                                            text:window.tr("Клавиши", "Keys")
                                            color: themeTokens.mist
                                            font.family: themeTokens.display
                                            font.pixelSize: Math.round(9.5 * window.textScale)
                                            font.bold: true
                                            font.letterSpacing: 1.6
                                            font.capitalization: Font.AllUppercase
                                        }
                                        Repeater {
                                            model: [
                                                { k: window.tr("Раздел", "Page"), v: "Ctrl+1…8" },
                                                { k: window.tr("Задача", "Task"), v: "Ctrl+L" },
                                                { k: window.tr("Поиск файла", "Find file"), v: "Ctrl+Shift+F" },
                                                { k: window.tr("Отправить", "Send"), v: "Ctrl+Enter" }
                                            ]
                                            delegate: Item {
                                                required property var modelData
                                                Layout.fillWidth: true
                                                Layout.preferredHeight: 22
                                                Layout.maximumHeight: 22
                                                Rectangle {
                                                    anchors.left: parent.left
                                                    anchors.right: parent.right
                                                    anchors.bottom: parent.bottom
                                                    height: 1
                                                    color: themeTokens.rule
                                                }
                                                RowLayout {
                                                    anchors.fill: parent
                                                    spacing: 8 * window.densityScale
                                                    Text {
                                                        text: modelData.k
                                                        color: themeTokens.mist
                                                        font.pixelSize: Math.round(10.5 * window.textScale)
                                                    }
                                                    Item { Layout.fillWidth: true }
                                                    Text {
                                                        text: modelData.v
                                                        color: themeTokens.graphite
                                                        font.family: themeTokens.mono
                                                        font.pixelSize: Math.round(10 * window.textScale)
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Item {
                        GlassCard {
                            anchors.fill: parent
                            inset: 10
                            SplitView {
                                id: projectSplit
                                anchors.fill: parent
                                orientation: Qt.Horizontal
                                handle: Rectangle {
                                    implicitWidth: 7
                                    color: "transparent"
                                    radius: 4
                                    Rectangle {
                                        anchors.centerIn: parent
                                        width: 2
                                        height: 34
                                        radius: 1
                                        color: projectSplit.resizing ? themeTokens.ember : themeTokens.verified
                                    }
                                }
                                onResizingChanged: {
                                    if (!resizing)
                                        bridge.saveWorkspaceLayout(fileBrowser.width,
                                                                   workspaceSplit.orientation === Qt.Horizontal ? terminalPane.width : terminalPane.height)
                                }
                                GlassCard {
                                    id: fileBrowser
                                    SplitView.minimumWidth: 160
                                    SplitView.preferredWidth: bridge.filePaneWidth
                                    SplitView.fillHeight: true
                                    inset: 9
                                    ColumnLayout {
                                        anchors.fill: parent
                                        spacing: 6 * window.densityScale
                                        RowLayout {
                                            Layout.fillWidth: true
                                            Text { text: window.tr("ПРОЕКТ", "PROJECT"); color: inkSoft; font.pixelSize: Math.round(9.5 * window.textScale); font.bold: true; font.letterSpacing: 1 }
                                            Item { Layout.fillWidth: true }
                                            Button {
                                                objectName: "createProjectFileButton"
                                                text: "+"
                                                Accessible.name: window.tr("Создать файл проекта", "Create project file")
                                                ToolTip.visible: hovered
                                                ToolTip.text: window.tr("Создать файл проекта", "Create project file")
                                                onClicked: newFileDialog.open()
                                                background: Rectangle { radius: themeTokens.radius; color: themeTokens.rule }
                                                contentItem: Text { text: parent.text; color: ocean; font.pixelSize: Math.round(14 * window.textScale); font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                            }
                                            Button {
                                                objectName: "refreshProjectFilesButton"
                                                text: "↻"
                                                Accessible.name: window.tr("Обновить список файлов", "Refresh file list")
                                                ToolTip.visible: hovered
                                                ToolTip.text: window.tr("Обновить список файлов", "Refresh file list")
                                                onClicked: bridge.refreshFiles()
                                                background: Rectangle { radius: themeTokens.radius; color: themeTokens.rule }
                                                contentItem: Text { text: parent.text; color: ocean; font.pixelSize: Math.round(12.5 * window.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                            }
                                        }
                                        Text { text: bridge.projectName; color: ink; font.pixelSize: Math.round(11.5 * window.textScale); font.bold: true; elide: Text.ElideMiddle; Layout.fillWidth: true }
                                        TextField {
                                            id: fileSearch
                                            objectName: "fileSearch"
                                            Layout.fillWidth: true
                                            placeholderText:window.tr("Найти файл в проекте…", "Find a file in the project…")
                                            selectByMouse: true
                                            color: ink
                                            font.pixelSize: Math.round(11 * window.textScale)
                                            leftPadding: 9
                                            background: Rectangle {
                                                radius: themeTokens.radius
                                                color: themeTokens.plate
                                                border.color: parent.activeFocus ? ocean : themeTokens.rule
                                                border.width: parent.activeFocus ? 2 : 1
                                            }
                                        }
                                        ListView {
                                            id: fileList
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true
                                            clip: true
                                            spacing: 3 * window.densityScale
                                            model: bridge.files.filter(function(row) {
                                                return row.path.toLowerCase().includes(fileSearch.text.trim().toLowerCase())
                                            })
                                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                                            delegate: Button {
                                                required property var modelData
                                                width: fileList.width
                                                height: 26
                                                onClicked: bridge.requestOpenFile(modelData.path, editor.text)
                                                background: Rectangle { radius: themeTokens.radius; color: bridge.openPath === modelData.path ? themeTokens.emberTint : "transparent" }
                                                contentItem: RowLayout {
                                                    spacing: 5 * window.densityScale
                                                    Text {
                                                        text: String(modelData.suffix || "").replace(".", "").slice(0, 4) || "·"
                                                        font.family: themeTokens.mono
                                                        color: themeTokens.mist
                                                        font.pixelSize: Math.round(10 * window.textScale)
                                                        Layout.preferredWidth: 24
                                                    }
                                                    Text { text: modelData.path; color: inkSoft; font.pixelSize: Math.round(10.5 * window.textScale); elide: Text.ElideMiddle; Layout.fillWidth: true }
                                                }
                                            }
                                        }
                                    }
                                }
                                SplitView {
                                    id: workspaceSplit
                                    SplitView.minimumWidth: 430
                                    SplitView.fillWidth: true
                                    SplitView.fillHeight: true
                                    orientation: bridge.terminalSideBySide ? Qt.Horizontal : Qt.Vertical
                                    handle: Rectangle {
                                        implicitHeight: 7
                                        implicitWidth: 7
                                        color: "transparent"
                                        radius: 4
                                        Rectangle {
                                            anchors.centerIn: parent
                                            width: bridge.terminalSideBySide ? 2 : 34
                                            height: bridge.terminalSideBySide ? 34 : 2
                                            radius: 1
                                            color: workspaceSplit.resizing ? themeTokens.ember : themeTokens.verified
                                        }
                                    }
                                    onResizingChanged: {
                                        if (!resizing)
                                            bridge.saveWorkspaceLayout(fileBrowser.width,
                                                                       orientation === Qt.Horizontal ? terminalPane.width : terminalPane.height)
                                    }
                                    spacing: 8 * window.densityScale
                                    GlassCard {
                                        id: editorPane
                                        surface: themeTokens.consoleBg
                                        SplitView.minimumWidth: 360
                                        SplitView.preferredWidth: Math.max(360, workspaceSplit.width - bridge.terminalSize)
                                        SplitView.fillWidth: !bridge.terminalSideBySide
                                        SplitView.minimumHeight: 250
                                        SplitView.preferredHeight: Math.max(250, workspaceSplit.height - bridge.terminalSize)
                                        SplitView.fillHeight: !bridge.terminalSideBySide
                                        inset: 9
                                        ColumnLayout {
                                            anchors.fill: parent
                                            spacing: 5 * window.densityScale
                                            ListView {
                                                id: editorTabs
                                                Layout.fillWidth: true
                                                Layout.preferredHeight: 30
                                                orientation: ListView.Horizontal
                                                spacing: 4 * window.densityScale
                                                clip: true
                                                model: bridge.openTabs
                                                delegate: Rectangle {
                                                    required property var modelData
                                                    width: Math.min(260, Math.max(132, tabText.implicitWidth + 47))
                                                    height: 28
                                                    radius: themeTokens.radius
                                                    color: modelData === bridge.openPath ? themeTokens.console3 : themeTokens.console2
                                                    border.color: modelData === bridge.openPath ? themeTokens.primaryTint : "#00000000"
                                                    RowLayout {
                                                        anchors.fill: parent
                                                        anchors.leftMargin: 3
                                                        anchors.rightMargin: 3
                                                        spacing: 1 * window.densityScale
                                                        Button {
                                                            Layout.fillWidth: true
                                                            text: modelData
                                                            onClicked: bridge.requestOpenFile(modelData, editor.text)
                                                            background: Item {}
                                                            contentItem: Text {
                                                                id: tabText
                                                                text: String(modelData).split("/").pop()
                                                                color: themeTokens.consoleInk
                                                                font.pixelSize: Math.round(11 * window.textScale)
                                                                elide: Text.ElideMiddle
                                                                verticalAlignment: Text.AlignVCenter
                                                            }
                                                        }
                                                        Button {
                                                            objectName: "closeFileTabButton_" + String(modelData).replace(/[^A-Za-z0-9_-]/g, "_")
                                                            text: "×"
                                                            ToolTip.visible: hovered
                                                            ToolTip.text: window.tr("Закрыть вкладку", "Close tab")
                                                            onClicked: bridge.closeFileTab(modelData, editor.text)
                                                            background: Item {}
                                                            contentItem: Text {
                                                                text: parent.text
                                                                color: themeTokens.consoleDim
                                                                font.pixelSize: Math.round(13 * window.textScale)
                                                                horizontalAlignment: Text.AlignHCenter
                                                                verticalAlignment: Text.AlignVCenter
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                            RowLayout {
                                                Layout.fillWidth: true
                                                Rectangle { width: 7; height: 7; radius: 4; color: bridge.openPath ? mint : themeTokens.ember }
                                                Text { text: bridge.openPath || window.tr("Выберите файл проекта", "Select a project file"); color: themeTokens.consoleInk; font.pixelSize: Math.round(11 * window.textScale); font.bold: true; elide: Text.ElideMiddle; Layout.fillWidth: true }
                                                Text { text: editor.text !== bridge.savedEditorContents ? window.tr("● изменён", "● modified") : ""; color: themeTokens.amber; font.pixelSize: Math.round(9.5 * window.textScale) }
                                                Button {
                                                    objectName: "fileDiffButton"
                                                    text: "Diff"
                                                    enabled: !!bridge.openPath
                                                    Accessible.name: window.tr("Показать изменения текущего файла", "Show changes in the current file")
                                                    onClicked: {
                                                        fileDiffPreview.text = window.formatDiff(bridge.editorDiff(editor.text))
                                                        fileDiffDialog.open()
                                                    }
                                                    background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.console2; border.color: themeTokens.line }
                                                    contentItem: Text { text: parent.text; color: parent.enabled ? themeTokens.aqua : themeTokens.consoleDim; font.pixelSize: Math.round(10.5 * window.textScale); font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 10; rightPadding: 10 }
                                                }
                                                Button {
                                                    objectName: "saveProjectFileButton"
                                                    text:window.tr("Сохранить", "Save")
                                                    Accessible.name: window.tr("Сохранить текущий файл · Ctrl+S", "Save current file · Ctrl+S")
                                                    enabled: !!bridge.openPath && editor.text !== bridge.savedEditorContents
                                                    onClicked: bridge.saveFile(editor.text)
                                                    background: Rectangle {
                                                        radius: themeTokens.radiusControl
                                                        color: parent.enabled ? themeTokens.console3 : themeTokens.console2
                                                        border.color: parent.activeFocus ? themeTokens.aqua : themeTokens.line
                                                        border.width: 1
                                                    }
                                                    contentItem: Text { text: parent.text; color: parent.enabled ? themeTokens.aqua : themeTokens.consoleDim; font.pixelSize: Math.round(10.5 * window.textScale); font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 10; rightPadding: 10 }
                                                }
                                            }
                                            TextArea {
                                                id: editor
                                                objectName: "projectEditor"
                                                Accessible.name: window.tr(
                                                    "Редактор файла " + (bridge.openPath || "не выбран"),
                                                    "File editor " + (bridge.openPath || "not selected")
                                                )
                                                Layout.fillWidth: true
                                                Layout.fillHeight: true
                                                text: bridge.editorContents
                                                enabled: !!bridge.openPath
                                                selectByMouse: true
                                                wrapMode: TextEdit.NoWrap
                                                color: themeTokens.consoleInk
                                                font.family: themeTokens.mono
                                                font.pixelSize: Math.round(11.5 * window.textScale)
                                                placeholderTextColor: themeTokens.consoleDim
                                                background: Rectangle {
                                                    radius: themeTokens.radiusControl
                                                    color: themeTokens.console2
                                                        border.color: parent.activeFocus ? themeTokens.aqua : themeTokens.line
                                                    border.width: 1
                                                }
                                                placeholderText: window.tr(
                                                    "Откройте текстовый файл слева или создайте новый.",
                                                    "Open a text file on the left or create a new one."
                                                )
                                                Component.onCompleted: text = bridge.editorContents
                                                Keys.onPressed: function(event) {
                                                    if (event.key === Qt.Key_S && (event.modifiers & Qt.ControlModifier)) {
                                                        if (bridge.openPath && editor.text !== bridge.savedEditorContents)
                                                            bridge.saveFile(editor.text)
                                                        event.accepted = true
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    GlassCard {
                                        id: terminalPane
                                        surface: themeTokens.consoleBg
                                        SplitView.minimumWidth: 280
                                        SplitView.preferredWidth: bridge.terminalSize
                                        SplitView.fillHeight: bridge.terminalSideBySide
                                        SplitView.minimumHeight: 150
                                        SplitView.preferredHeight: bridge.terminalSize
                                        SplitView.fillWidth: bridge.terminalSideBySide
                                        inset: 8
                                        ColumnLayout {
                                            anchors.fill: parent
                                            spacing: 5 * window.densityScale
                                            RowLayout {
                                                Layout.fillWidth: true
                                                Text {
                                                    text: window.tr("ТЕРМИНАЛ · ", "TERMINAL · ")
                                                        + (bridge.terminalShell || bridge.projectName)
                                                    color: themeTokens.consoleInk
                                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                                    font.bold: true
                                                    font.letterSpacing: 0.7
                                                }
                                                Item { Layout.fillWidth: true }
                                                Button {
                                                objectName: "terminalLayoutButton"
                                                text: bridge.terminalSideBySide ? window.tr("Снизу", "Below") : window.tr("Справа", "Beside")
                                                    ToolTip.visible: hovered
                                                ToolTip.text: bridge.terminalSideBySide
                                                    ? window.tr("Переместить консоль вниз", "Move terminal below")
                                                    : window.tr("Переместить консоль вправо", "Move terminal beside editor")
                                                    onClicked: bridge.setTerminalSideBySide(!bridge.terminalSideBySide)
                                                    background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.console3; border.color: themeTokens.line }
                                                    contentItem: Text { text: parent.text; color: themeTokens.aqua; font.pixelSize: Math.round(10.5 * window.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 8; rightPadding: 8 }
                                                }
                                                Button {
                                                    visible: bridge.terminalRunning
                                                    text: bridge.terminalBusy
                                                        ? window.tr("Остановить", "Stop")
                                                        : window.tr("Закрыть оболочку", "Close shell")
                                                    Accessible.name: text
                                                    onClicked: bridge.stopCommand()
                                                    background: Rectangle { radius: themeTokens.radius; color: themeTokens.plate }
                                                    contentItem: Text { text: parent.text; color: themeTokens.fail; font.pixelSize: Math.round(9.5 * window.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 8; rightPadding: 8 }
                                                }
                                            }
                                            TextArea {
                                                id: terminalLog
                                                Layout.fillWidth: true
                                                Layout.fillHeight: true
                                                text: bridge.terminalOutput
                                                readOnly: true
                                                selectByMouse: true
                                                wrapMode: TextEdit.NoWrap
                                                color: themeTokens.consoleInk
                                                font.family: themeTokens.mono
                                                font.pixelSize: Math.round(11 * window.textScale)
                                                background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.console2 }
                                                onTextChanged: cursorPosition = length
                                            }
                                            RowLayout {
                                                Layout.fillWidth: true
                                                spacing: 6 * window.densityScale
                                                TextField {
                                                    id: terminalCommand
                                                    objectName: "terminalCommand"
                                                    Accessible.name: bridge.terminalBusy
                                                        ? window.tr("Ввод для активной команды", "Input for the active command")
                                                        : window.tr("Команда для выполнения в терминале", "Command to run in the terminal")
                                                    Layout.fillWidth: true
                                                    placeholderText: bridge.terminalBusy
                                                        ? window.tr("Ввод в поток активной команды…", "Send input to the active command…")
                                                        : window.tr("Команда в проекте · например pytest", "Command in the project · for example pytest")
                                                    color: themeTokens.consoleInk
                                                    font.family: themeTokens.mono
                                                    font.pixelSize: Math.round(11 * window.textScale)
                                                    placeholderTextColor: themeTokens.consoleDim
                                                    background: Rectangle {
                                                        radius: themeTokens.radiusControl
                                                        color: themeTokens.console2
                                                        border.color: parent.activeFocus ? themeTokens.aqua : themeTokens.line
                                                        border.width: 1
                                                    }
                                                    Keys.onReturnPressed: {
                                                        bridge.runCommand(text)
                                                        clear()
                                                    }
                                                }
                                                Button {
                                                    objectName: "terminalRunButton"
                                                    text: bridge.terminalBusy
                                                        ? window.tr("Отправить", "Send")
                                                        : bridge.terminalReady
                                                            ? window.tr("Выполнить", "Run")
                                                            : bridge.terminalRunning
                                                                ? window.tr("Подключение…", "Connecting…")
                                                                : window.tr("Выполнить", "Run")
                                                    Accessible.name: bridge.terminalBusy
                                                        ? window.tr("Отправить ввод в активную команду", "Send input to the active command")
                                                        : window.tr("Выполнить команду в терминале", "Run command in the terminal")
                                                    enabled: terminalCommand.text.trim().length > 0
                                                        && (!bridge.terminalRunning || bridge.terminalReady || bridge.terminalBusy)
                                                    onClicked: {
                                                        bridge.runCommand(terminalCommand.text)
                                                        terminalCommand.clear()
                                                    }
                                                    background: Rectangle {
                                                        radius: themeTokens.radius
                                                        color: parent.enabled ? themeTokens.ember : themeTokens.rule
                                                        border.color: parent.activeFocus ? themeTokens.ink : "transparent"
                                                        border.width: parent.activeFocus ? 2 : 0
                                                    }
                                                    contentItem: Text { text: parent.text; color: "white"; font.pixelSize: Math.round(10.5 * window.textScale); font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 10; rightPadding: 10 }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        Connections {
                            target: bridge
                            function onEditorChanged() {
                                editor.text = bridge.editorContents
                            }
                        }
                    }
                    DeepResearchPage {
                        id: explorePage
                        objectName: "deepResearchPage"
                        language: window.lang
                        textScale: window.textScale
                        densityScale: window.densityScale
                        onSourceRequested: function(path) {
                            bridge.requestOpenFile(path, editor.text)
                            window.navigateTo("files", false)
                        }
                    }
                    OrchestratePage {
                        id: orchestratePage
                        language: window.lang
                        textScale: window.textScale
                        onOpenChatRequested: window.navigateTo("forge", false)
                        onHistoryRequested: goalHistoryDialog.open()
                    }
                }

                GlassCard {
                    visible: window.activePage !== "settings" && window.activePage !== "files"
                    Layout.fillWidth: true
                    Layout.preferredHeight: (bridge.mode === "chat" ? 172
                        : bridge.mode === "goal" ? 228 : 194)
                        + (bridge.mode !== "chat" ? 30 : 0)
                        + (bridge.attachments.length ? 32 : 0)
                        + (window.adaptiveWorkspaceIntent !== "general" ? 30 : 0)
                    Layout.leftMargin: 16 * window.densityScale
                    Layout.rightMargin: 16 * window.densityScale
                    Layout.bottomMargin: 14 * window.densityScale
                    inset: 14
                    glass: true
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 7 * window.densityScale
                        RowLayout {
                            Layout.fillWidth: true
                            visible: bridge.mode !== "chat"
                            Text {
                        font.family: themeTokens.text
                        text: window.tr("ЗАДАЧА", "TASK")
                                color: inkSoft
                                font.pixelSize: Math.round(9.5 * window.textScale)
                                font.bold: true
                                font.letterSpacing: 1.1 }
                            Item { Layout.fillWidth: true }
                            Text {
                        font.family: themeTokens.text
                            text: bridge.mode === "goal" && !bridge.goalRuntimeReady
                                ? (bridge.goalRuntimeRunning
                                    ? window.tr("Подготавливаю среду Goal…", "Preparing Goal environment…")
                                    : window.tr("Для режима Goal проверьте среду в настройках", "Check the Goal environment in Settings"))
                                : window.tr("Enter · отправить   Shift+Enter · новая строка",
                                            "Enter · send   Shift+Enter · new line")
                                color: inkSoft
                                font.pixelSize: Math.round(9.5 * window.textScale)
                                elide: Text.ElideRight
                            }
                        }
                        RowLayout {
                            objectName: "goalModeBadge"
                            visible: bridge.mode === "goal"
                            Layout.fillWidth: true
                            Layout.preferredHeight: 24 * window.densityScale
                            spacing: 7 * window.densityScale
                            Rectangle {
                                Layout.preferredWidth: 54 * window.densityScale
                                Layout.preferredHeight: 21 * window.densityScale
                                radius: themeTokens.radiusPill
                                color: themeTokens.mintTint
                                border.color: themeTokens.lineStrong
                                Text {
                                    anchors.centerIn: parent
                                    text: "GOAL"
                                    color: themeTokens.primaryDeep
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(8.5 * window.textScale)
                                    font.bold: true
                                    font.letterSpacing: 0.7
                                }
                            }
                            Text {
                                text: window.tr(
                                    "Цель активна · результат будет проверен",
                                    "Goal active · the outcome will be verified"
                                )
                                color: themeTokens.ink3
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(9 * window.textScale)
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            Button {
                                objectName: "leaveGoalModeButton"
                                Layout.preferredWidth: 24 * window.densityScale
                                Layout.preferredHeight: 22 * window.densityScale
                                text: "×"
                                Accessible.name: window.tr("Вернуться в чат", "Return to Chat")
                                onClicked: bridge.setMode("chat")
                                background: Rectangle {
                                    radius: themeTokens.radiusPill
                                    color: parent.hovered ? themeTokens.surface3 : "transparent"
                                }
                                contentItem: Text {
                                    text: parent.text
                                    color: themeTokens.ink3
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(14 * window.textScale)
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                        RowLayout {
                            objectName: "agentProfileSelector"
                            visible: bridge.mode !== "chat"
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 25 * window.densityScale : 0
                            spacing: 7 * window.densityScale
                            Text {
                                text: window.tr("ПРОФИЛЬ", "PROFILE")
                                color: themeTokens.ink4
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(7.5 * window.textScale)
                                font.letterSpacing: 0.7
                            }
                            ComboBox {
                                id: agentProfileCombo
                                objectName: "agentProfileCombo"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 25 * window.densityScale
                                model: window.agentProfileChoices
                                textRole: "name"
                                valueRole: "id"
                                currentIndex: {
                                    for (let i = 0; i < window.agentProfileChoices.length; i++) {
                                        if (window.agentProfileChoices[i].id === bridge.activeAgentProfileId)
                                            return i
                                    }
                                    return 0
                                }
                                enabled: !bridge.running
                                onActivated: function(index) {
                                    bridge.setActiveAgentProfile(String(window.agentProfileChoices[index].id))
                                }
                                background: Rectangle {
                                    radius: themeTokens.radiusPill
                                    color: themeTokens.surface2
                                    border.color: parent.activeFocus ? themeTokens.lineStrong : themeTokens.lineSoft
                                }
                                contentItem: Text {
                                    leftPadding: 10
                                    rightPadding: 26
                                    text: agentProfileCombo.displayText
                                    color: themeTokens.primaryDeep
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(9 * window.textScale)
                                    elide: Text.ElideRight
                                    verticalAlignment: Text.AlignVCenter
                                }
                                indicator: Text {
                                    x: agentProfileCombo.width - width - 9
                                    y: (agentProfileCombo.height - height) / 2
                                    text: "⌄"
                                    color: themeTokens.ink3
                                    font.pixelSize: Math.round(12 * window.textScale)
                                }
                                delegate: ItemDelegate {
                                    required property var modelData
                                    required property int index
                                    width: agentProfileCombo.width
                                    height: 32
                                    highlighted: agentProfileCombo.highlightedIndex === index
                                    contentItem: Text {
                                        text: modelData.name
                                        color: agentProfileCombo.highlightedIndex === index
                                            ? themeTokens.primaryDeep : themeTokens.ink2
                                        font.family: themeTokens.text
                                        font.pixelSize: Math.round(9.5 * window.textScale)
                                        elide: Text.ElideRight
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    background: Rectangle {
                                        radius: themeTokens.radiusControl
                                        color: parent.highlighted ? themeTokens.primaryTint : "transparent"
                                    }
                                }
                                popup: Popup {
                                    y: agentProfileCombo.height + 4
                                    width: agentProfileCombo.width
                                    implicitHeight: Math.min(contentItem.implicitHeight, 210)
                                    padding: 4
                                    contentItem: ListView {
                                        clip: true
                                        implicitHeight: contentHeight
                                        model: agentProfileCombo.popup.visible
                                            ? agentProfileCombo.delegateModel : null
                                        currentIndex: agentProfileCombo.highlightedIndex
                                    }
                                    background: Rectangle {
                                        radius: themeTokens.radiusControl
                                        color: themeTokens.plate
                                        border.color: themeTokens.lineStrong
                                    }
                                }
                            }
                            Button {
                                objectName: "manageAgentProfilesButton"
                                Layout.preferredWidth: 76 * window.densityScale
                                Layout.preferredHeight: 25 * window.densityScale
                                text: window.tr("Профили", "Profiles")
                                enabled: !bridge.running
                                onClicked: agentProfileDialog.open()
                                background: Rectangle {
                                    radius: themeTokens.radiusPill
                                    color: parent.hovered ? themeTokens.mintTint : themeTokens.surface
                                    border.color: themeTokens.line
                                }
                                contentItem: Text {
                                    text: parent.text
                                    color: themeTokens.primaryDeep
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(8.5 * window.textScale)
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                        RowLayout {
                            objectName: "adaptiveWorkspaceNotice"
                            visible: window.adaptiveWorkspaceIntent !== "general"
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 25 * window.densityScale : 0
                            spacing: 7 * window.densityScale
                            Rectangle {
                                Layout.preferredWidth: 7 * window.densityScale
                                Layout.preferredHeight: 7 * window.densityScale
                                radius: themeTokens.radiusPill
                                color: window.adaptiveWorkspaceIntent === "automation"
                                    ? themeTokens.aqua
                                    : window.adaptiveWorkspaceIntent === "codebase"
                                        ? themeTokens.verified : themeTokens.primary
                            }
                            Text {
                                text: window.workspaceIntentLabel(window.adaptiveWorkspaceIntent)
                                    + " · " + (window.appliedWorkspaceIntent === window.adaptiveWorkspaceIntent
                                        ? window.tr("среда адаптирована", "workspace adapted")
                                        : window.tr("предложение", "suggestion"))
                                color: themeTokens.primaryDeep
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(9 * window.textScale)
                                font.bold: true
                                elide: Text.ElideRight
                            }
                            Text {
                                text: window.workspaceIntentReason(window.adaptiveWorkspaceIntent)
                                color: themeTokens.ink3
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(8.5 * window.textScale)
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            Button {
                                objectName: "adaptiveWorkspaceAgentAction"
                                visible: bridge.mode === "chat"
                                    && (window.adaptiveWorkspaceIntent === "codebase"
                                        || window.adaptiveWorkspaceIntent === "automation")
                                Layout.preferredWidth: 62 * window.densityScale
                                Layout.preferredHeight: 23 * window.densityScale
                                text: "Agent →"
                                Accessible.name: window.tr(
                                    "Переключиться в Agent для работы с проектом",
                                    "Switch to Agent for project work"
                                )
                                enabled: !bridge.running
                                onClicked: bridge.setMode("agent")
                                background: Rectangle {
                                    radius: themeTokens.radiusPill
                                    color: parent.hovered ? themeTokens.aquaTint : themeTokens.surface2
                                    border.color: themeTokens.line
                                }
                                contentItem: Text {
                                    text: parent.text
                                    color: themeTokens.aquaInk
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(8.5 * window.textScale)
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                            Button {
                                objectName: "adaptiveWorkspaceAction"
                                Layout.preferredWidth: 76 * window.densityScale
                                Layout.preferredHeight: 23 * window.densityScale
                                text: window.appliedWorkspaceIntent === window.adaptiveWorkspaceIntent
                                    ? window.tr("Вернуть", "Undo") : window.tr("Применить", "Apply")
                                onClicked: {
                                    if (window.appliedWorkspaceIntent === window.adaptiveWorkspaceIntent)
                                        window.restoreAdaptiveWorkspace()
                                    else
                                        window.applyAdaptiveWorkspace(window.adaptiveWorkspaceIntent)
                                }
                                background: Rectangle {
                                    radius: themeTokens.radiusPill
                                    color: parent.hovered ? themeTokens.mintTint : themeTokens.surface2
                                    border.color: themeTokens.line
                                }
                                contentItem: Text {
                                    text: parent.text
                                    color: themeTokens.primaryDeep
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(8.5 * window.textScale)
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                        RowLayout {
                            objectName: "composerProjectContext"
                            visible: window.appliedWorkspaceIntent !== "writing"
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 26 * window.densityScale : 0
                            spacing: 7 * window.densityScale
                            Rectangle {
                                Layout.preferredWidth: 22 * window.densityScale
                                Layout.preferredHeight: 22 * window.densityScale
                                radius: themeTokens.radiusPill
                                color: themeTokens.aquaTint
                                Text {
                                    anchors.centerIn: parent
                                    text: "▣"
                                    color: themeTokens.aquaInk
                                    font.pixelSize: Math.round(10 * window.textScale)
                                }
                            }
                            Text {
                                text: window.tr("КОНТЕКСТ", "CONTEXT")
                                color: themeTokens.ink4
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(7.5 * window.textScale)
                                font.letterSpacing: 0.7
                            }
                            Button {
                                objectName: "composerProjectContextButton"
                                text: bridge.projectName
                                Accessible.name: window.tr(
                                    "Контекст проекта. Нажмите, чтобы сменить проект.",
                                    "Project context. Activate to switch projects."
                                )
                                ToolTip.visible: hovered
                                ToolTip.text: bridge.projectPath
                                onClicked: projectFolderDialog.open()
                                Layout.maximumWidth: 220 * window.densityScale
                                implicitHeight: 24 * window.densityScale
                                background: Rectangle {
                                    radius: themeTokens.radiusPill
                                    color: parent.hovered ? themeTokens.mintTint : themeTokens.surface2
                                    border.color: parent.hovered ? themeTokens.lineStrong : themeTokens.lineSoft
                                }
                                contentItem: Text {
                                    text: parent.text
                                    color: themeTokens.primaryDeep
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(9 * window.textScale)
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    leftPadding: 10
                                    rightPadding: 10
                                    elide: Text.ElideRight
                                }
                            }
                            Text {
                                text: bridge.files.length + window.tr(" файлов в проекте", " project files")
                                color: themeTokens.ink3
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(8.5 * window.textScale)
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                        AttachmentChips {
                            objectName: "composerAttachmentChips"
                            visible: bridge.attachments.length > 0
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 26 * window.densityScale : 0
                            attachments: bridge.attachments
                            language: window.lang
                            densityScale: window.densityScale
                            textScale: window.textScale
                            onRemoveRequested: function(path) { bridge.removeAttachment(path) }
                        }
                        TextArea {
                            id: composer
                            objectName: "taskComposer"
                            Accessible.name: window.tr("Описание задачи", "Task description")
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 58 * window.densityScale
                            onTextChanged: {
                                const cleaned = bridge.consumeGoalCommand(text)
                                if (cleaned !== text) {
                                    text = cleaned
                                    cursorPosition = text.length
                                    return
                                }
                                workspaceIntentTimer.restart()
                            }
                            placeholderText: window.lang === "en"
                                ? (bridge.mode === "chat"
                                    ? "Message… · type /goal to define an outcome"
                                    : bridge.mode === "agent"
                                        ? "Describe a task for one development cycle…"
                                        : "State a goal — Goal verifies the result and keeps improving…")
                                : (bridge.mode === "chat"
                                    ? "Сообщение… · введите /goal для постановки цели"
                                    : bridge.mode === "agent"
                                        ? "Опишите задачу для одного цикла разработки…"
                                        : "Сформулируйте цель — Goal проверит результат и продолжит улучшения…")
                            placeholderTextColor: themeTokens.ink3
                            wrapMode: TextEdit.Wrap
                            selectByMouse: true
                            color: themeTokens.ink
                            font.pixelSize: Math.round(14 * window.textScale)
                            background: Rectangle {
                                radius: themeTokens.radiusControl
                                color: parent.activeFocus ? themeTokens.surface2 : "transparent"
                                border.color: "transparent"
                                border.width: 0
                            }
                            Keys.onPressed: function(event) {
                                if (event.key === Qt.Key_Return && !(event.modifiers & Qt.ShiftModifier)) {
                                    window.sendPrompt()
                                    event.accepted = true
                                }
                            }
                        }
                        Rectangle {
                            Layout.fillWidth: true
                            height: 1
                            color: themeTokens.lineSoft
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6 * window.densityScale
                            ModeSelector {
                                id: modeSelector
                                objectName: "taskModeSelector"
                                objectNamePrefix: "modeButton_"
                                Layout.preferredWidth: (mainWorkspacePane.width < 620 ? 132 : 140)
                                    * window.densityScale
                                Layout.preferredHeight: 34 * window.densityScale
                                mode: bridge.mode
                                language: window.lang
                                textScale: window.textScale * window.densityScale
                                densityScale: window.densityScale
                                enabled: !bridge.running
                                onModeSelected: function(value) { bridge.setMode(value) }
                            }
                            Button {
                                objectName: "attachFileButton"
                                visible: window.appliedWorkspaceIntent !== "writing"
                                    || bridge.attachments.length > 0
                                Layout.preferredWidth: 44 * window.densityScale
                                Layout.preferredHeight: 40 * window.densityScale
                                enabled: !bridge.running
                                Accessible.name: window.tr("Прикрепить файл", "Attach a file")
                                ToolTip.visible: hovered
                                ToolTip.text: Accessible.name
                                onClicked: attachmentFileDialog.open()
                                background: Rectangle {
                                    radius: themeTokens.radiusControl
                                    color: parent.hovered ? themeTokens.surface2 : "transparent"
                                }
                                contentItem: SmartIcon {
                                    objectName: "attachFileIcon"
                                    anchors.centerIn: parent
                                    width: 28 * window.densityScale
                                    height: 28 * window.densityScale
                                    kind: "attach"
                                    accent: themeTokens.ink3
                                    ring: "transparent"
                                }
                            }
                            Button {
                                objectName: "subagentsButton"
                                Layout.preferredWidth: 44 * window.densityScale
                                Layout.preferredHeight: 40 * window.densityScale
                                visible: bridge.mode !== "chat"
                                    && window.appliedWorkspaceIntent !== "writing"
                                enabled: !bridge.running
                                checkable: true
                                checked: bridge.useSubagents
                                Accessible.name: window.tr(
                                    "Использовать субагентов для этой задачи",
                                    "Use subagents for this task")
                                ToolTip.visible: hovered
                                ToolTip.text: Accessible.name
                                onToggled: bridge.setUseSubagents(checked)
                                background: Rectangle {
                                    radius: themeTokens.radiusControl
                                    color: parent.checked ? themeTokens.primaryTint
                                        : parent.hovered ? themeTokens.surface2 : "transparent"
                                    border.color: parent.checked ? themeTokens.lineStrong : "transparent"
                                }
                                contentItem: SmartIcon {
                                    objectName: "subagentsIcon"
                                    anchors.centerIn: parent
                                    width: 28 * window.densityScale
                                    height: 28 * window.densityScale
                                    kind: "subagents"
                                    accent: parent.checked ? themeTokens.primaryDeep : themeTokens.ink3
                                    selected: parent.checked
                                    ring: "transparent"
                                }
                            }
                            Button {
                                objectName: "goalHistoryButton"
                                visible: bridge.mode === "goal"
                                enabled: !bridge.running
                                text:window.tr("История", "History")
                                Accessible.name: window.tr(
                                    "Открыть сохранённые цели проекта",
                                    "Open saved goals for this project"
                                )
                                onClicked: {
                                    bridge.refreshGoalHistory()
                                    goalHistoryDialog.open()
                                }
                                background: Rectangle {
                                    radius: themeTokens.radius
                                    color: themeTokens.plate
                                    border.color: themeTokens.rule
                                }
                                contentItem: Text {
                                    font.family: themeTokens.text
                                    text: parent.text
                                    color: inkSoft
                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    leftPadding: 10
                                    rightPadding: 10
                                }
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                        font.family: themeTokens.text
                        text: window.displayStatusText
                                color: inkSoft
                                font.pixelSize: Math.round(9.5 * window.textScale)
                                elide: Text.ElideRight
                                Layout.maximumWidth: 180 }
                            Button {
                                text: bridge.running ? window.tr("В работе…", "Running…")
                                    : bridge.mode === "chat"
                                        ? window.tr("Отправить", "Send")
                                        : window.tr("Запустить", "Run")
                                Accessible.name: bridge.running
                                    ? window.tr("Задача выполняется", "Task is running")
                                    : window.tr("Отправить задачу", "Send task")
                                enabled: !bridge.running
                                    && (composer.text.trim().length > 0 || bridge.attachments.length > 0)
                                    && (bridge.mode !== "goal" || bridge.goalRuntimeReady)
                                onClicked: window.sendPrompt()
                                Layout.preferredHeight: 38 * window.densityScale
                                Layout.preferredWidth: 120 * window.densityScale
                                background: Rectangle { radius: themeTokens.radiusControl
                                    color: parent.enabled ? themeTokens.primary : themeTokens.lineStrong
                                    border.color: parent.activeFocus ? themeTokens.ink : "transparent"
                                    border.width: parent.activeFocus ? 2 : 0 }
                                contentItem: Text {
                        font.family: themeTokens.text
                        text: !bridge.running && bridge.mode === "chat"
                            ? parent.text + "  ↑"
                            : parent.text
                                    color: "white"
                                    font.pixelSize: Math.round(11 * window.textScale)
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    leftPadding: 14
                                    rightPadding: 14
                                    topPadding: 6
                                    bottomPadding: 6 }
                            }
                        }
                    }
                }
            }

            Rectangle {
                id: contextPane
                objectName: "contextPane"
                visible: window.contextVisible && window.width >= 1120
                SplitView.minimumWidth: window.width < 1320 ? 260 : 300
                SplitView.preferredWidth: window.width < 1320
                    ? Math.max(280, Math.min(300, bridge.contextPanelWidth))
                    : Math.max(320, bridge.contextPanelWidth)
                SplitView.maximumWidth: 400
                SplitView.fillHeight: true
                color: themeTokens.plate
                border.color: themeTokens.lineStrong
                border.width: 1
                radius: themeTokens.radius
                gradient: Gradient {
                    GradientStop { position: 0.0; color: themeTokens.surface2 }
                    GradientStop { position: 1.0; color: themeTokens.surface }
                }

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 1
                    height: 1
                    color: themeTokens.glassHighlight
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 12 * window.densityScale

                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: window.tr("ЖИВОЙ КОНТЕКСТ", "LIVE CONTEXT")
                            color: themeTokens.ink
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(10 * window.textScale)
                            font.letterSpacing: 1.4
                            font.capitalization: Font.AllUppercase
                        }
                        Item { Layout.fillWidth: true }
                        Rectangle {
                            width: 5
                            height: 5
                            radius: 3
                            color: bridge.running ? themeTokens.ember : themeTokens.verified
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 5 * window.densityScale
                        Text {
                            text: window.contextTaskTitle
                            color: themeTokens.mist
                            font.family: themeTokens.display
                            font.pixelSize: Math.round(10 * window.textScale)
                            font.bold: true
                            font.letterSpacing: 1.4
                            font.capitalization: Font.AllUppercase
                        }
                        Text {
                            text: window.contextTaskText
                            color: themeTokens.ink
                            font.pixelSize: Math.round(12.5 * window.textScale)
                            wrapMode: Text.WordWrap
                            maximumLineCount: 4
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Text {
                            text: window.contextTaskStatus
                            color: themeTokens.ink3
                            font.pixelSize: Math.round(10.5 * window.textScale)
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 5 * window.densityScale
                        Rectangle {
                            Layout.fillWidth: true
                            height: 3
                            radius: 1
                            color: themeTokens.surface3
                            Rectangle {
                                objectName: "contextProgressFill"
                                height: 3
                                radius: 1
                                width: parent.width * window.displayProgressPercent / 100
                                color: window.displayProgressPercent >= 100 ? themeTokens.mint : themeTokens.aqua
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                text: window.displayProgress
                                color: themeTokens.graphite
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(9.5 * window.textScale)
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: window.displayRunStatus
                                color: themeTokens.mist
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(9.5 * window.textScale)
                                elide: Text.ElideRight
                                Layout.maximumWidth: 140
                            }
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6 * window.densityScale
                        Text {
                            text: window.tr("КОНТЕКСТ", "CONTEXT")
                            color: themeTokens.mist
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(9.5 * window.textScale)
                            font.letterSpacing: 1.2
                        }
                        Flow {
                            Layout.fillWidth: true
                            Layout.preferredHeight: implicitHeight
                            spacing: 6 * window.densityScale
                            Repeater {
                                model: [
                                    { value: bridge ? bridge.projectName : "", tint: themeTokens.surface3, accent: themeTokens.graphite },
                                    { value: bridge ? bridge.modelName : "", tint: themeTokens.aquaTint, accent: themeTokens.aqua },
                                    { value: bridge ? String(bridge.mode || "").toUpperCase() : "", tint: themeTokens.mintTint, accent: themeTokens.mint }
                                ]
                                delegate: Rectangle {
                                    required property var modelData
                                    width: contextChipLabel.implicitWidth + 28
                                    height: 23
                                    radius: themeTokens.radiusPill
                                    color: modelData.tint
                                    border.color: themeTokens.line
                                    border.width: 1
                                    Row {
                                        anchors.fill: parent
                                        anchors.leftMargin: 8
                                        anchors.rightMargin: 8
                                        spacing: 5
                                        Rectangle {
                                            width: 5
                                            height: 5
                                            radius: 3
                                            anchors.verticalCenter: parent.verticalCenter
                                            color: modelData.accent
                                        }
                                        Text {
                                            id: contextChipLabel
                                            text: modelData.value
                                            color: themeTokens.ink2
                                            font.family: themeTokens.mono
                                            font.pixelSize: Math.round(9 * window.textScale)
                                            anchors.verticalCenter: parent.verticalCenter
                                        }
                                    }
                                }
                            }
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 5 * window.densityScale
                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                text: window.tr("РАСХОД / ЛИМИТ", "USAGE / LIMIT")
                                color: themeTokens.mist
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(9.5 * window.textScale)
                                font.letterSpacing: 1.2
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: {
                                    const used = window.contextRunTokens
                                    const cap = window.contextTaskBudget
                                    const usedText = window.formatCount(used)
                                    return cap > 0
                                        ? usedText + " / " + window.formatCount(cap)
                                        : (used > 0 ? usedText + window.tr(" токенов", " tokens") : window.tr("Без лимита", "Unlimited"))
                                }
                                color: themeTokens.ink2
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(9.5 * window.textScale)
                            }
                        }
                        Rectangle {
                            visible: window.contextTaskBudget > 0
                            Layout.fillWidth: true
                            height: 5
                            radius: 3
                            color: themeTokens.lineSoft
                            Rectangle {
                                width: parent.width * Math.min(
                                    1,
                                    window.contextRunTokens / Math.max(1, window.contextTaskBudget)
                                )
                                height: parent.height
                                radius: parent.radius
                                color: window.contextRunTokens >= window.contextTaskBudget
                                    ? themeTokens.amber : themeTokens.mint
                            }
                        }
                        Text {
                            text: window.tr(
                                "Токены последнего прогона · лимит из настроек",
                                "Last-run tokens · limit from settings"
                            )
                            color: themeTokens.ink3
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(9.5 * window.textScale)
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        height: 1
                        color: themeTokens.rule
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2 * window.densityScale
                        RowLayout {
                            Layout.fillWidth: true
                            Layout.bottomMargin: 4 * window.densityScale
                            Text {
                                text: window.contextActivityTitle
                                color: themeTokens.mist
                                font.family: themeTokens.display
                                font.pixelSize: Math.round(10 * window.textScale)
                                font.bold: true
                                font.letterSpacing: 1.4
                                font.capitalization: Font.AllUppercase
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: contextEvents.count
                                color: themeTokens.mist
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(9.5 * window.textScale)
                            }
                        }
                        Repeater {
                            id: contextEvents
                            model: window.recentActivity.slice(
                                0, window.height < 820 || contextPane.width < 320 ? 6 : 8
                            )
                            delegate: Item {
                                required property var modelData
                                Layout.fillWidth: true
                                Layout.preferredHeight: window.height < 820 || contextPane.width < 320 ? 38 : 46
                                Rectangle {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: parent.bottom
                                    height: 1
                                    color: themeTokens.rule
                                }
                                RowLayout {
                                    anchors.fill: parent
                                    spacing: 9 * window.densityScale
                                    Rectangle {
                                        width: 7
                                        height: 7
                                        radius: 4
                                        color: modelData.kind === "error" ? themeTokens.fail
                                             : modelData.kind === "tool" ? themeTokens.ember : themeTokens.verified
                                    }
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 1 * window.densityScale
                                        Text {
                                            text: {
                                                const data = modelData.data || {}
                                                return String(data.node || data.name
                                                    || (modelData.kind === "error" ? window.tr("Ошибка", "Error")
                                                    : modelData.kind === "tool" ? window.tr("Инструмент", "Tool")
                                                    : window.tr("Этап", "Stage")))
                                            }
                                            color: modelData.kind === "error" ? themeTokens.fail : themeTokens.ink
                                            font.family: themeTokens.text
                                            font.pixelSize: Math.round(11 * window.textScale)
                                            font.bold: true
                                            elide: Text.ElideRight
                                            Layout.fillWidth: true
                                        }
                                        Text {
                                            text: {
                                                const data = modelData.data || {}
                                                return String(data.summary || modelData.text
                                                    || data.status || data.event || "")
                                            }
                                            color: themeTokens.graphite
                                            font.family: themeTokens.text
                                            font.pixelSize: Math.round(9.5 * window.textScale)
                                            elide: Text.ElideRight
                                            Layout.fillWidth: true
                                        }
                                    }
                                }
                            }
                        }
                        Text {
                            visible: contextEvents.count === 0
                            text: bridge.running
                                ? window.tr("Подготовка задачи и контекста…", "Preparing the task and context…")
                                : window.tr("События выполнения появятся здесь.", "Execution events will appear here.")
                            color: themeTokens.graphite
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(10.5 * window.textScale)
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                            Layout.topMargin: 6 * window.densityScale
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }
        }

        Text {
            font.family: themeTokens.text
            Layout.leftMargin: chatHistoryPanel.width + 20
            Layout.rightMargin: 22 * window.densityScale
            Layout.topMargin: 6 * window.densityScale
            Layout.bottomMargin: 8 * window.densityScale
            text: !bridge.goalRuntimeReady
                ? window.goalRuntimeStatusLabel(bridge.goalRuntimeStatus)
                : bridge.startupMessage || "GigaForge · live cycle: plan → act → verify → review"
            color: inkSoft
            font.pixelSize: Math.round(9.5 * window.textScale)
            elide: Text.ElideRight
            Layout.fillWidth: true
            visible: !bridge.goalRuntimeReady || !!bridge.startupMessage || window.activePage !== "settings"
        }
    }

    FloatingWindow {
        id: terminalFloatingWindow
        objectName: "floatingTerminalWindow"
        visible: window.terminalWindowOpen
        stackLevel: window.terminalWindowZ
        onVisibleChanged: if (visible) window.raiseFloatingWindow("terminal")
        onRaiseRequested: window.raiseFloatingWindow("terminal")
        title: window.tr("Консоль проекта", "Project terminal") + " · " + bridge.projectName
        iconText: ">_"
        accent: bridge.terminalBusy ? themeTokens.amber : themeTokens.aqua
        closeLabel: window.tr("Закрыть панель", "Close panel")
        minimizeLabel: window.tr("Свернуть панель", "Minimize panel")
        maximizeLabel: window.tr("Развернуть панель", "Maximize panel")
        minWidth: 370
        minHeight: 270
        maxWidth: Math.max(370, window.width - 24)
        maxHeight: Math.max(270, window.height - 70)
        width: Math.min(Math.max(minWidth, window.terminalWindowGeometry[2]), window.width - 32)
        height: Math.min(Math.max(minHeight, window.terminalWindowGeometry[3]), window.height - 94)
        x: window.terminalWindowGeometry[0] < 0
            ? Math.max(16, window.width - width - 30)
            : Math.max(16, Math.min(window.terminalWindowGeometry[0], window.width - width - 16))
        y: window.terminalWindowGeometry[1] < 0
            ? Math.max(60, window.height - height - 82)
            : Math.max(54, Math.min(window.terminalWindowGeometry[1], window.height - height - 52))
        onCloseRequested: window.terminalWindowOpen = false
        onMinimizeRequested: window.terminalWindowOpen = false
        onGeometryCommitted: function(x, y, width, height) {
            bridge.saveFloatingTerminalGeometry(x, y, width, height)
        }

        ColumnLayout {
            anchors.fill: parent
            spacing: 7 * window.densityScale

            RowLayout {
                Layout.fillWidth: true
                spacing: 8 * window.densityScale
                Rectangle {
                    width: 6
                    height: 6
                    radius: 3
                    color: bridge.terminalBusy ? themeTokens.amber
                        : bridge.terminalReady ? themeTokens.mint : themeTokens.ink4
                }
                Text {
                    Layout.fillWidth: true
                    text: bridge.terminalBusy
                        ? window.tr("Команда выполняется", "Command running")
                        : bridge.terminalReady
                            ? bridge.terminalShell + " · " + bridge.projectName
                            : window.tr("Оболочка запустится с первой команды", "Shell starts with the first command")
                    color: themeTokens.ink3
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(9.5 * window.textScale)
                    elide: Text.ElideMiddle
                }
                Button {
                    visible: bridge.terminalRunning
                    text: bridge.terminalBusy
                        ? window.tr("Остановить", "Stop")
                        : window.tr("Закрыть оболочку", "Close shell")
                    Accessible.name: text
                    onClicked: bridge.stopCommand()
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: themeTokens.surface
                        border.color: themeTokens.line
                    }
                    contentItem: Text {
                        text: parent.text
                        color: bridge.terminalBusy ? themeTokens.fail : themeTokens.ink3
                        font.pixelSize: Math.round(9 * window.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 8
                        rightPadding: 8
                    }
                }
            }

            TextArea {
                id: floatingTerminalLog
                objectName: "floatingTerminalLog"
                Layout.fillWidth: true
                Layout.fillHeight: true
                text: bridge.terminalOutput
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.NoWrap
                color: themeTokens.consoleInk
                font.family: themeTokens.mono
                font.pixelSize: Math.round(10.5 * window.textScale)
                background: Rectangle {
                    radius: themeTokens.radiusInner
                    color: themeTokens.consoleBg
                    border.color: themeTokens.lineSoft
                }
                onTextChanged: cursorPosition = length
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 7 * window.densityScale
                TextField {
                    id: terminalQuickCommand
                    objectName: "floatingTerminalCommand"
                    Accessible.name: bridge.terminalBusy
                        ? window.tr("Ввод для активной команды", "Input for the active command")
                        : window.tr("Команда для выполнения в терминале", "Command to run in the terminal")
                    Layout.fillWidth: true
                    placeholderText: bridge.terminalBusy
                        ? window.tr("Ввод в поток активной команды…", "Send input to the active command…")
                        : window.tr("Команда в проекте · например pytest", "Command in the project · for example pytest")
                    color: themeTokens.consoleInk
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(10.5 * window.textScale)
                    placeholderTextColor: themeTokens.consoleDim
                    background: Rectangle {
                        radius: themeTokens.radiusInner
                        color: themeTokens.consoleBg
                        border.color: parent.activeFocus ? themeTokens.aqua : themeTokens.line
                    }
                    Keys.onReturnPressed: {
                        bridge.runCommand(text)
                        clear()
                    }
                }
                Button {
                    objectName: "floatingTerminalRunButton"
                    text: bridge.terminalBusy
                        ? window.tr("Отправить", "Send")
                        : bridge.terminalRunning && !bridge.terminalReady
                            ? window.tr("Подключение…", "Connecting…")
                            : window.tr("Выполнить", "Run")
                    enabled: terminalQuickCommand.text.trim().length > 0
                        && (!bridge.terminalRunning || bridge.terminalReady || bridge.terminalBusy)
                    onClicked: {
                        bridge.runCommand(terminalQuickCommand.text)
                        terminalQuickCommand.clear()
                    }
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: parent.enabled ? themeTokens.primary : themeTokens.surface
                    }
                    contentItem: Text {
                        text: parent.text
                        color: parent.enabled ? themeTokens.actionInk : themeTokens.ink4
                        font.pixelSize: Math.round(9.5 * window.textScale)
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 10
                        rightPadding: 10
                    }
                }
            }
        }
    }

    FloatingWindow {
        id: chatFloatingWindow
        objectName: "floatingChatWindow"
        visible: window.chatWindowOpen
        stackLevel: window.chatWindowZ
        onVisibleChanged: if (visible) window.raiseFloatingWindow("chat")
        onRaiseRequested: window.raiseFloatingWindow("chat")
        title: window.tr("Рабочий чат", "Workspace chat") + " · " + bridge.projectName
        iconText: "\uE8BD"
        iconFont: "Segoe MDL2 Assets"
        accent: themeTokens.aqua
        closeLabel: window.tr("Закрыть чат", "Close chat")
        minimizeLabel: window.tr("Свернуть чат", "Minimize chat")
        maximizeLabel: window.tr("Развернуть чат", "Maximize chat")
        minWidth: 350
        minHeight: 300
        maxWidth: Math.max(350, window.width - 24)
        maxHeight: Math.max(300, window.height - 70)
        width: Math.min(Math.max(minWidth, window.chatWindowGeometry[2]), window.width - 32)
        height: Math.min(Math.max(minHeight, window.chatWindowGeometry[3]), window.height - 94)
        x: window.chatWindowGeometry[0] < 0
            ? Math.max(16, window.width - width - 30)
            : Math.max(16, Math.min(window.chatWindowGeometry[0], window.width - width - 16))
        y: window.chatWindowGeometry[1] < 0
            ? 66
            : Math.max(54, Math.min(window.chatWindowGeometry[1], window.height - height - 52))
        onCloseRequested: window.chatWindowOpen = false
        onMinimizeRequested: window.chatWindowOpen = false
        onGeometryCommitted: function(x, y, width, height) {
            bridge.saveFloatingChatGeometry(x, y, width, height)
        }

        readonly property var transcriptEvents: bridge.events.filter(function(event) {
            const data = event.data || {}
            return event.kind === "command" || !!String(event.text || "").trim()
                || !!data.summary || !!data.status || !!data.event
        })

        ColumnLayout {
            anchors.fill: parent
            spacing: 8 * window.densityScale

            RowLayout {
                Layout.fillWidth: true
                spacing: 7 * window.densityScale
                Rectangle {
                    width: 7
                    height: 7
                    radius: 4
                    color: bridge.running ? themeTokens.aqua : themeTokens.mint
                }
                Text {
                    Layout.fillWidth: true
                    text: bridge.running
                        ? window.tr("Задача выполняется", "Task in progress")
                        : window.lang === "en"
                            ? ({ chat: "Chat", agent: "Agent run", goal: "Goal" }[bridge.mode] || bridge.mode)
                            : ({ chat: "Разговор", agent: "Запуск агента", goal: "Цель Goal" }[bridge.mode] || bridge.mode)
                    color: themeTokens.ink3
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(10 * window.textScale)
                    elide: Text.ElideRight
                }
                Text {
                    text: window.tr("Общий поток событий", "Shared live transcript")
                    color: themeTokens.ink4
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(9 * window.textScale)
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: themeTokens.radiusInner
                color: themeTokens.light ? "#F3FFFFFF" : themeTokens.surface2
                border.color: themeTokens.lineSoft
                clip: true

                ListView {
                    id: floatingChatTranscript
                    anchors.fill: parent
                    anchors.margins: 10 * window.densityScale
                    clip: true
                    spacing: 8 * window.densityScale
                    model: chatFloatingWindow.transcriptEvents
                    onCountChanged: Qt.callLater(function() { positionViewAtEnd() })
                    delegate: Rectangle {
                        required property var modelData
                        property var eventData: modelData.data || ({})
                        property bool userMessage: eventData.role === "user" || modelData.kind === "command"
                        width: floatingChatTranscript.width
                        height: Math.max(42, Math.min(messageBody.implicitHeight, 160) + 26)
                        radius: themeTokens.radiusInner
                        color: userMessage ? themeTokens.primaryTint : themeTokens.surface2
                        border.color: userMessage ? themeTokens.lineStrong : themeTokens.lineSoft

                        Column {
                            anchors.fill: parent
                            anchors.margins: 9 * window.densityScale
                            spacing: 4 * window.densityScale
                            Text {
                                text: parent.parent.userMessage
                                    ? window.tr("ВЫ", "YOU")
                                    : String(parent.parent.eventData.role || parent.parent.eventData.node
                                        || parent.parent.modelData.kind || "GigaForge").toUpperCase()
                                color: parent.parent.userMessage ? themeTokens.primary : themeTokens.aquaInk
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(8.5 * window.textScale)
                                font.bold: true
                                font.letterSpacing: 0.7
                            }
                            Text {
                                id: messageBody
                                width: parent.width
                                text: String(parent.parent.modelData.text
                                    || parent.parent.eventData.summary
                                    || parent.parent.eventData.status
                                    || parent.parent.eventData.event || "")
                                color: themeTokens.ink2
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(10.5 * window.textScale)
                                wrapMode: Text.Wrap
                                maximumLineCount: 9
                                elide: Text.ElideRight
                            }
                        }
                    }
                }

                ColumnLayout {
                    anchors.centerIn: parent
                    width: Math.min(parent.width - 36, 270)
                    spacing: 7
                    visible: floatingChatTranscript.count === 0
                    Item {
                        Layout.preferredWidth: 36
                        Layout.preferredHeight: 36
                        Layout.alignment: Qt.AlignHCenter
                        Rectangle {
                            width: 25
                            height: 25
                            radius: 13
                            anchors.centerIn: parent
                            color: themeTokens.aquaTint
                            border.color: themeTokens.lineStrong
                        }
                        Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            anchors.centerIn: parent
                            color: themeTokens.aqua
                        }
                        Rectangle {
                            width: 4
                            height: 4
                            radius: 2
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.rightMargin: 4
                            anchors.topMargin: 4
                            color: themeTokens.lime
                        }
                    }
                    Text {
                        text: window.tr("Начните с вопроса или задачи", "Start with a question or task")
                        color: themeTokens.ink2
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(12 * window.textScale)
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                        Layout.fillWidth: true
                    }
                    Text {
                        text: window.tr("Сообщения здесь синхронизированы с основным рабочим пространством.",
                                        "Messages here stay in sync with the main workspace.")
                        color: themeTokens.ink3
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(9.5 * window.textScale)
                        wrapMode: Text.WordWrap
                        horizontalAlignment: Text.AlignHCenter
                        Layout.fillWidth: true
                    }
                }
            }

            RowLayout {
                objectName: "floatingGoalModeBadge"
                visible: bridge.mode === "goal"
                Layout.fillWidth: true
                Layout.preferredHeight: 24 * window.densityScale
                spacing: 7 * window.densityScale
                Rectangle {
                    Layout.preferredWidth: 54 * window.densityScale
                    Layout.preferredHeight: 21 * window.densityScale
                    radius: themeTokens.radiusPill
                    color: themeTokens.mintTint
                    border.color: themeTokens.lineStrong
                    Text {
                        anchors.centerIn: parent
                        text: "GOAL"
                        color: themeTokens.primaryDeep
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(8.5 * window.textScale)
                        font.bold: true
                        font.letterSpacing: 0.7
                    }
                }
                Text {
                    text: window.tr(
                        "Цель активна · результат будет проверен",
                        "Goal active · the outcome will be verified"
                    )
                    color: themeTokens.ink3
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(9 * window.textScale)
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Button {
                    objectName: "floatingLeaveGoalModeButton"
                    Layout.preferredWidth: 24 * window.densityScale
                    Layout.preferredHeight: 22 * window.densityScale
                    text: "×"
                    Accessible.name: window.tr("Вернуться в чат", "Return to Chat")
                    onClicked: bridge.setMode("chat")
                    background: Rectangle {
                        radius: themeTokens.radiusPill
                        color: parent.hovered ? themeTokens.surface3 : "transparent"
                    }
                    contentItem: Text {
                        text: parent.text
                        color: themeTokens.ink3
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(14 * window.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }

            AttachmentChips {
                objectName: "floatingAttachmentChips"
                visible: bridge.attachments.length > 0
                Layout.fillWidth: true
                Layout.preferredHeight: visible ? 26 * window.densityScale : 0
                attachments: bridge.attachments
                language: window.lang
                densityScale: window.densityScale
                textScale: window.textScale
                onRemoveRequested: function(path) { bridge.removeAttachment(path) }
            }

            TextArea {
                id: floatingChatComposer
                objectName: "floatingChatComposer"
                Accessible.name: window.tr("Сообщение или задача", "Message or task")
                Layout.fillWidth: true
                Layout.preferredHeight: 72 * window.densityScale
                onTextChanged: {
                    const cleaned = bridge.consumeGoalCommand(text)
                    if (cleaned !== text) {
                        text = cleaned
                        cursorPosition = text.length
                    }
                }
                placeholderText: bridge.mode === "chat"
                    ? window.tr("Сообщение… · /goal для цели", "Message… · /goal for Goal mode")
                    : window.tr("Опишите задачу или желаемый результат…", "Describe the task or outcome…")
                placeholderTextColor: themeTokens.ink3
                color: themeTokens.ink
                font.family: themeTokens.text
                font.pixelSize: Math.round(11.5 * window.textScale)
                wrapMode: TextEdit.Wrap
                selectByMouse: true
                background: Rectangle {
                    radius: themeTokens.radiusInner
                    color: parent.activeFocus ? themeTokens.surface2 : "transparent"
                    border.color: "transparent"
                    border.width: 0
                }
                Keys.onPressed: function(event) {
                    if (event.key === Qt.Key_Return && !(event.modifiers & Qt.ShiftModifier)) {
                        window.sendFloatingChatPrompt()
                        event.accepted = true
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 5 * window.densityScale
                Item {
                    objectName: "floatingChatModeSelector"
                    Layout.preferredWidth: 140 * window.densityScale
                    Layout.preferredHeight: 34 * window.densityScale
                    ModeSelector {
                        objectName: "floatingChatModeControl"
                        objectNamePrefix: "floatingChatModeButton_"
                        anchors.fill: parent
                        mode: bridge.mode
                        language: window.lang
                        textScale: window.textScale * window.densityScale
                        densityScale: window.densityScale
                        enabled: !bridge.running
                        onModeSelected: function(value) { bridge.setMode(value) }
                    }
                }
                Button {
                    objectName: "floatingAttachFileButton"
                    Layout.preferredWidth: 44 * window.densityScale
                    Layout.preferredHeight: 40 * window.densityScale
                    enabled: !bridge.running
                    Accessible.name: window.tr("Прикрепить файл", "Attach a file")
                    ToolTip.visible: hovered
                    ToolTip.text: Accessible.name
                    onClicked: attachmentFileDialog.open()
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: parent.hovered ? themeTokens.surface2 : "transparent"
                    }
                    contentItem: SmartIcon {
                        objectName: "floatingAttachFileIcon"
                        anchors.centerIn: parent
                        width: 28 * window.densityScale
                        height: 28 * window.densityScale
                        kind: "attach"
                        accent: themeTokens.ink3
                        ring: "transparent"
                    }
                }
                Item { Layout.fillWidth: true }
                Button {
                    objectName: "floatingSubagentsButton"
                    visible: bridge.mode !== "chat"
                    enabled: !bridge.running
                    checkable: true
                    checked: bridge.useSubagents
                    Layout.preferredWidth: 44 * window.densityScale
                    Layout.preferredHeight: 40 * window.densityScale
                    Accessible.name: window.tr("Использовать субагентов", "Use subagents")
                    ToolTip.visible: hovered
                    ToolTip.text: Accessible.name
                    onToggled: bridge.setUseSubagents(checked)
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: parent.checked ? themeTokens.primaryTint
                            : parent.hovered ? themeTokens.surface2 : "transparent"
                        border.color: parent.checked ? themeTokens.lineStrong : "transparent"
                    }
                    contentItem: SmartIcon {
                        objectName: "floatingSubagentsIcon"
                        anchors.centerIn: parent
                        width: 28 * window.densityScale
                        height: 28 * window.densityScale
                        kind: "subagents"
                        accent: parent.checked ? themeTokens.primaryDeep : themeTokens.ink3
                        selected: parent.checked
                        ring: "transparent"
                    }
                }
                Button {
                    objectName: "floatingChatSendButton"
                    text: bridge.running ? window.tr("Выполняется", "Working") : window.tr("Отправить", "Send")
                    Layout.preferredHeight: 38 * window.densityScale
                    enabled: (floatingChatComposer.text.trim().length > 0 || bridge.attachments.length > 0)
                        && !bridge.running
                    Accessible.name: text
                    onClicked: window.sendFloatingChatPrompt()
                    background: Rectangle {
                        radius: themeTokens.radiusPill
                        color: parent.enabled ? themeTokens.primary : themeTokens.surface
                        border.color: parent.enabled ? themeTokens.lineStrong : themeTokens.line
                    }
                    contentItem: Text {
                        text: parent.text + (bridge.running ? "" : "  ↑")
                        color: parent.enabled ? themeTokens.actionInk : themeTokens.ink4
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(9.5 * window.textScale)
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 10
                        rightPadding: 10
                    }
                }
            }
        }
    }

    Popup {
        id: commandPalette
        objectName: "commandPalette"
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: 12
        width: Math.min(window.width * 0.55, 560)
        height: Math.min(window.height * 0.72, 560)
        anchors.centerIn: parent
        Overlay.modal: Rectangle { color: "#3A04100C" }
        background: Rectangle { radius: themeTokens.radius; color: themeTokens.surface2; border.color: themeTokens.lineStrong }
        onOpened: { commandSearch.clear(); Qt.callLater(function() { commandSearch.forceActiveFocus() }) }
        contentItem: ColumnLayout {
            spacing: 8 * window.densityScale
            TextField {
                id: commandSearch
                Layout.fillWidth: true
                Layout.preferredHeight: 42 * window.densityScale
                placeholderText: window.tr("Перейти к разделу или действию…", "Go to a page or action…")
                selectByMouse: true
                color: themeTokens.ink
                placeholderTextColor: themeTokens.ink3
                font.family: themeTokens.text
                font.pixelSize: Math.round(12 * window.textScale)
                background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.surface; border.color: commandSearch.activeFocus ? themeTokens.primary : themeTokens.controlLine }
                onTextChanged: commandResults.model = commandPalette.commandItems.filter(function(item) {
                    const label = window.lang === "en" ? item.en : item.ru
                    const query = commandSearch.text.trim().toLowerCase()
                    return !query || label.toLowerCase().indexOf(query) >= 0
                })
                Keys.onReturnPressed: if (commandResults.count > 0) window.runCommand(commandResults.model[0].id)
            }
            ListView {
                id: commandResults
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: commandPalette.commandItems
                delegate: Button {
                    required property var modelData
                    width: commandResults.width
                    height: 42 * window.densityScale
                    text: window.lang === "en" ? modelData.en : modelData.ru
                    onClicked: window.runCommand(modelData.id)
                    background: Rectangle { radius: themeTokens.radiusControl; color: parent.hovered ? themeTokens.primaryTint : "transparent"; border.color: parent.hovered ? themeTokens.line : "transparent" }
                    contentItem: RowLayout {
                        Text { text: parent.parent.text; color: themeTokens.ink; font.family: themeTokens.text; font.pixelSize: Math.round(11 * window.textScale); Layout.fillWidth: true }
                        Text { text: modelData.key; color: themeTokens.ink4; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * window.textScale) }
                    }
                }
            }
        }
        property var commandItems: [
            { id: "overview", ru: "Обзор", en: "Overview", key: "Ctrl+1" },
            { id: "forge", ru: "Чат · история диалогов", en: "Chat · conversation history", key: "Ctrl+2" },
            { id: "explore", ru: "Explore · глубокое исследование", en: "Explore · deep research", key: "Ctrl+3" },
            { id: "orchestrate", ru: "Orchestrate · ход запуска", en: "Orchestrate · run timeline", key: "Ctrl+4" },
            { id: "files", ru: "Файлы и редактор", en: "Files and editor", key: "Ctrl+5" },
            { id: "agents", ru: "Агенты", en: "Agents", key: "Ctrl+6" },
            { id: "monitoring", ru: "Мониторинг", en: "Monitoring", key: "Ctrl+7" },
            { id: "settings", ru: "Настройки", en: "Settings", key: "Ctrl+8" },
            { id: "focusTask", ru: "Перейти к вводу задачи", en: "Focus task composer", key: "Ctrl+L" },
            { id: "newChat", ru: "Новый чат", en: "New chat", key: "Ctrl+N" },
            { id: "toggleChat", ru: "Открыть плавающий чат", en: "Open floating chat", key: "Ctrl+Shift+C" },
            { id: "toggleTerminal", ru: "Открыть консоль проекта", en: "Open project terminal", key: "Ctrl+Shift+J" },
            { id: "theme", ru: "Сменить тему", en: "Cycle theme", key: "" },
            { id: "help", ru: "Сочетания клавиш", en: "Keyboard shortcuts", key: "Ctrl+/" }
        ]
    }

    Popup {
        id: keyboardHelpPopup
        objectName: "keyboardHelpPopup"
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: 16
        width: Math.min(window.width * 0.56, 520)
        height: Math.min(window.height * 0.72, 530)
        anchors.centerIn: parent
        Overlay.modal: Rectangle { color: "#2E16181A" }
        background: Rectangle { radius: 25; color: themeTokens.plate; border.color: themeTokens.rule }
        contentItem: ColumnLayout {
            spacing: 10 * window.densityScale
            RowLayout {
                Layout.fillWidth: true
                Text {
                    text: window.tr("Сочетания клавиш", "Keyboard shortcuts")
                    color: ink
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(15 * window.textScale)
                    font.bold: true
                }
                Item { Layout.fillWidth: true }
                Button {
                    text: "×"
                    Accessible.name: window.tr("Закрыть подсказки клавиш", "Close keyboard shortcuts")
                    onClicked: keyboardHelpPopup.close()
                    background: Rectangle { radius: themeTokens.radius; color: themeTokens.plate }
                    contentItem: Text {
                        text: parent.text
                        color: inkSoft
                        font.pixelSize: Math.round(17 * window.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    Layout.preferredWidth: 34
                    Layout.preferredHeight: 34
                }
            }
            Text {
                text:window.tr("Навигация и быстрые действия доступны с клавиатуры.", "Navigation and quick actions are available from the keyboard.")
                color: inkSoft
                font.family: themeTokens.text
                font.pixelSize: Math.round(11 * window.textScale)
                Layout.fillWidth: true
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4 * window.densityScale
            Repeater {
                model: [
                    { key: "Ctrl+1", action: "Обзор", actionEn: "Overview" },
                    { key: "Ctrl+2", action: "Чат · история диалогов", actionEn: "Chat · conversation history" },
                    { key: "Ctrl+3", action: "Explore · глубокое исследование", actionEn: "Explore · deep research" },
                    { key: "Ctrl+4", action: "Orchestrate · ход запуска", actionEn: "Orchestrate · run timeline" },
                    { key: "Ctrl+5", action: "Файлы проекта", actionEn: "Project files" },
                    { key: "Ctrl+6", action: "Агенты", actionEn: "Agents" },
                    { key: "Ctrl+7", action: "Мониторинг", actionEn: "Monitoring" },
                    { key: "Ctrl+8", action: "Настройки", actionEn: "Settings" },
                    { key: "Ctrl+K", action: "Поиск и команды", actionEn: "Search and commands" },
                    { key: "Ctrl+L", action: "Перейти к полю задачи", actionEn: "Focus task composer" },
                    { key: "Ctrl+Shift+F", action: "Найти файл", actionEn: "Find a file" },
                    { key: "Ctrl+N", action: "Новый чат", actionEn: "New chat" },
                    { key: "Ctrl+Shift+C", action: "Открыть плавающий чат", actionEn: "Open floating chat" },
                    { key: "Ctrl+Shift+J", action: "Открыть консоль проекта", actionEn: "Open project terminal" },
                    { key: "Ctrl+S", action: "Сохранить файл в редакторе", actionEn: "Save file in the editor" },
                    { key: "Enter / Shift+Enter", action: "Отправить / новая строка", actionEn: "Send / new line" },
                    { key: "Ctrl+/", action: "Эта подсказка", actionEn: "This guide" }
                ]
                delegate: RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 12 * window.densityScale
                    Rectangle {
                        radius: 9
                        color: themeTokens.plate
                        border.color: themeTokens.rule
                        Layout.preferredHeight: 27
                        Layout.preferredWidth: keyLabel.implicitWidth + 18
                        Text {
                            id: keyLabel
                            anchors.centerIn: parent
                            text: modelData.key
                            color: ocean
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(10.5 * window.textScale)
                            font.bold: true
                        }
                    }
                    Text {
                        text: window.lang === "en" ? modelData.actionEn : modelData.action
                        color: ink
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(11 * window.textScale)
                        Layout.fillWidth: true
                    }
                }
            }
            }
        }
    }
    Popup {
        id: goalHistoryDialog
        objectName: "goalHistoryDialog"
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: 16
        width: Math.min(window.width * 0.68, 720)
        height: Math.min(
            window.height * 0.66,
            Math.max(260, 190 + Math.min(bridge.goalHistory.length, 4) * 112)
        )
        anchors.centerIn: parent
        Overlay.modal: Rectangle { color: "#2E16181A" }
        background: Rectangle {
            radius: 25
            color: themeTokens.plate
            border.color: themeTokens.rule
        }
        contentItem: ColumnLayout {
            spacing: 10 * window.densityScale
            RowLayout {
                Layout.fillWidth: true
                Text {
                    text:window.tr("Сохранённые цели", "Saved goals")
                    color: ink
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(15 * window.textScale)
                    font.bold: true
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: bridge.projectName
                    color: inkSoft
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(10.5 * window.textScale)
                    elide: Text.ElideMiddle
                    Layout.maximumWidth: 220
                }
                Button {
                    text: "×"
                    Accessible.name: window.tr("Закрыть историю целей", "Close goal history")
                    onClicked: goalHistoryDialog.close()
                    background: Rectangle { radius: themeTokens.radius; color: themeTokens.plate }
                    contentItem: Text {
                        text: parent.text
                        color: inkSoft
                        font.pixelSize: Math.round(17 * window.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    Layout.preferredWidth: 34
                    Layout.preferredHeight: 34
                }
            }
            Text {
                text: window.tr(
                    "Каждая цель хранит свои циклы, изменения и результаты проверки в выбранном проекте. Незавершённую цель можно продолжить с последней контрольной точки.",
                    "Each goal keeps its cycles, changes and verification results in the selected project. An unfinished goal can continue from its latest checkpoint."
                )
                color: inkSoft
                font.pixelSize: Math.round(11 * window.textScale)
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Text {
                    anchors.centerIn: parent
                    visible: bridge.goalHistory.length === 0
                    text:window.tr("В этом проекте пока нет сохранённых целей", "No saved goals in this project yet")
                    color: inkSoft
                    font.pixelSize: Math.round(11.5 * window.textScale)
                }
                ListView {
                    id: goalHistoryList
                    objectName: "goalHistoryList"
                    anchors.fill: parent
                    visible: bridge.goalHistory.length > 0
                    clip: true
                    spacing: 8 * window.densityScale
                    model: bridge.goalHistory
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    delegate: Rectangle {
                        required property var modelData
                        width: ListView.view.width
                        height: 100
                        radius: themeTokens.radius
                        color: themeTokens.plate
                        border.color: themeTokens.rule
                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 12 * window.densityScale
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 5 * window.densityScale
                                Text {
                                    text: modelData.goal || window.tr("Цель без описания", "Untitled goal")
                                    color: ink
                                    font.pixelSize: Math.round(11.5 * window.textScale)
                                    font.bold: true
                                    wrapMode: Text.WordWrap
                                    maximumLineCount: 2
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: window.goalStatusLabel(modelData.status)
                                        + window.tr("  ·  циклов ", "  ·  cycles ")
                                        + (modelData.cycle_count || 0) + "  ·  " + (modelData.updated_at || "")
                                    color: inkSoft
                                    font.pixelSize: Math.round(9.5 * window.textScale)
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    visible: !!modelData.last_summary
                                    text: modelData.last_summary || ""
                                    color: inkSoft
                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                    maximumLineCount: 1
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                            }
                            Button {
                                objectName: "resumeGoalButton"
                                visible: !!modelData.resumable
                                enabled: bridge.goalRuntimeReady && !bridge.running
                                text:window.tr("Продолжить", "Continue")
                                Accessible.name: window.tr("Продолжить цель: ", "Continue goal: ")
                                    + (modelData.goal || window.tr("без описания", "untitled"))
                                onClicked: {
                                    goalHistoryDialog.close()
                                    bridge.resumeGoal(modelData.goal_id)
                                }
                                background: Rectangle {
                                    radius: themeTokens.radius
                                    color: parent.enabled ? themeTokens.emberTint : themeTokens.plate
                                    border.color: themeTokens.rule
                                }
                                contentItem: Text {
                                    font.family: themeTokens.text
                                    text: parent.text
                                    color: ocean
                                    font.pixelSize: Math.round(10.5 * window.textScale)
                                    font.bold: true
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    leftPadding: 10
                                    rightPadding: 10
                                }
                            }
                            Text {
                                visible: !modelData.resumable
                                text:window.tr("Завершено", "Completed")
                                color: themeTokens.verified
                                font.pixelSize: Math.round(10.5 * window.textScale)
                                font.bold: true
                            }
                        }
                    }
                }
            }
        }
    }
    Dialog {
        id: agentProfileDialog
        objectName: "agentProfileDialog"
        title: window.tr("Профили агентов", "Agent profiles")
        modal: true
        standardButtons: Dialog.NoButton
        width: Math.min(window.width - 40, 560)
        height: Math.min(window.height - 60, 560)
        anchors.centerIn: parent
        property string editingProfileId: ""
        function startNewProfile() {
            editingProfileId = ""
            agentProfileName.clear()
            agentProfileInstructions.clear()
            agentProfileSubagents.checked = false
            agentProfileName.forceActiveFocus()
        }
        function editProfile(profile) {
            editingProfileId = String(profile.id || "")
            agentProfileName.text = String(profile.name || "")
            agentProfileInstructions.text = String(profile.instructions || "")
            agentProfileSubagents.checked = Boolean(profile.useSubagents)
        }
        background: Rectangle {
            radius: themeTokens.radius
            color: themeTokens.plate
            border.color: themeTokens.lineStrong
        }
        contentItem: ColumnLayout {
            spacing: 9 * window.densityScale
            RowLayout {
                Layout.fillWidth: true
                Text {
                    text: window.tr("СОХРАНЁННЫЕ ПРОФИЛИ", "SAVED PROFILES")
                    color: themeTokens.ink3
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(8.5 * window.textScale)
                    font.letterSpacing: 0.9
                    Layout.fillWidth: true
                }
                Button {
                    objectName: "newAgentProfileButton"
                    enabled: !bridge.running
                    text: window.tr("Новый профиль", "New profile")
                    onClicked: agentProfileDialog.startNewProfile()
                    background: Rectangle {
                        radius: themeTokens.radiusPill
                        color: parent.hovered ? themeTokens.mintTint : themeTokens.surface2
                        border.color: themeTokens.line
                    }
                    contentItem: Text {
                        text: parent.text
                        color: themeTokens.primaryDeep
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(9 * window.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 10
                        rightPadding: 10
                    }
                }
            }
            ListView {
                objectName: "agentProfilesList"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(bridge.agentProfiles.length * 38, 152)
                visible: bridge.agentProfiles.length > 0
                clip: true
                spacing: 2
                model: bridge.agentProfiles
                delegate: Item {
                    required property var modelData
                    width: ListView.view.width
                    height: 36
                    RowLayout {
                        anchors.fill: parent
                        spacing: 6
                        Text {
                            text: modelData.name
                            color: modelData.id === bridge.activeAgentProfileId
                                ? themeTokens.primaryDeep : themeTokens.ink2
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(10 * window.textScale)
                            font.bold: modelData.id === bridge.activeAgentProfileId
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Text {
                            text: modelData.useSubagents
                                ? window.tr("субагенты", "subagents") : window.tr("один агент", "single agent")
                            color: themeTokens.ink4
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(8 * window.textScale)
                        }
                        Button {
                            objectName: "editAgentProfileButton_" + modelData.id
                            text: window.tr("Изменить", "Edit")
                            onClicked: agentProfileDialog.editProfile(modelData)
                            background: Rectangle {
                                radius: themeTokens.radiusControl
                                color: parent.hovered ? themeTokens.surface3 : themeTokens.surface
                                border.color: themeTokens.line
                            }
                            contentItem: Text {
                                text: parent.text
                                color: themeTokens.ink2
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(8.5 * window.textScale)
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                                leftPadding: 7
                                rightPadding: 7
                            }
                        }
                        Button {
                            objectName: "deleteAgentProfileButton_" + modelData.id
                            enabled: !bridge.running
                            text: "×"
                            Accessible.name: window.tr("Удалить профиль", "Delete profile")
                            onClicked: {
                                bridge.deleteAgentProfile(String(modelData.id))
                                if (agentProfileDialog.editingProfileId === String(modelData.id))
                                    agentProfileDialog.startNewProfile()
                            }
                            background: Rectangle {
                                radius: themeTokens.radiusControl
                                color: parent.hovered ? themeTokens.failTint : "transparent"
                            }
                            contentItem: Text {
                                text: parent.text
                                color: themeTokens.fail
                                font.pixelSize: Math.round(13 * window.textScale)
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }
                }
            }
            Text {
                visible: bridge.agentProfiles.length === 0
                text: window.tr(
                    "Создайте роль с собственными инструкциями и настройкой субагентов.",
                    "Create a role with custom instructions and a subagent preference."
                )
                color: themeTokens.ink3
                font.family: themeTokens.text
                font.pixelSize: Math.round(9.5 * window.textScale)
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            TextField {
                id: agentProfileName
                objectName: "agentProfileName"
                placeholderText: window.tr("Имя профиля", "Profile name")
                color: themeTokens.ink
                font.family: themeTokens.text
                font.pixelSize: Math.round(10 * window.textScale)
                background: Rectangle {
                    radius: themeTokens.radiusControl
                    color: themeTokens.surface
                    border.color: parent.activeFocus ? themeTokens.lineStrong : themeTokens.line
                }
            }
            TextArea {
                id: agentProfileInstructions
                objectName: "agentProfileInstructions"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 100
                placeholderText: window.tr(
                    "Роль, подход, ограничения и критерии качества…",
                    "Role, approach, boundaries, and quality criteria…"
                )
                color: themeTokens.ink
                font.family: themeTokens.text
                font.pixelSize: Math.round(10 * window.textScale)
                wrapMode: TextEdit.Wrap
                selectByMouse: true
                background: Rectangle {
                    radius: themeTokens.radiusControl
                    color: themeTokens.surface
                    border.color: parent.activeFocus ? themeTokens.lineStrong : themeTokens.line
                }
            }
            RowLayout {
                Layout.fillWidth: true
                CheckBox {
                    id: agentProfileSubagents
                    objectName: "agentProfileSubagents"
                    text: window.tr("Предпочитать субагентов", "Prefer subagents")
                    checked: false
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: window.tr("Подтверждения остаются общими в Настройках.",
                                    "Approval policy remains global in Settings.")
                    color: themeTokens.ink4
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(8.5 * window.textScale)
                    elide: Text.ElideRight
                    Layout.maximumWidth: 200
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    enabled: !bridge.running
                    text: window.tr("Отмена", "Cancel")
                    onClicked: agentProfileDialog.close()
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: parent.hovered ? themeTokens.surface3 : themeTokens.surface
                        border.color: themeTokens.line
                    }
                    contentItem: Text {
                        text: parent.text
                        color: themeTokens.ink2
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(9.5 * window.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 12
                        rightPadding: 12
                    }
                }
                Button {
                    objectName: "saveAgentProfileButton"
                    enabled: !bridge.running
                    text: window.tr("Сохранить профиль", "Save profile")
                    onClicked: {
                        if (bridge.saveAgentProfile(
                                agentProfileDialog.editingProfileId,
                                agentProfileName.text,
                                agentProfileInstructions.text,
                                agentProfileSubagents.checked
                            ))
                            agentProfileDialog.close()
                    }
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: themeTokens.primary
                    }
                    contentItem: Text {
                        text: parent.text
                        color: themeTokens.primaryDeep
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(9.5 * window.textScale)
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 12
                        rightPadding: 12
                    }
                }
            }
        }
    }

    Dialog {
        id: approvalDialog
        objectName: "approvalDialog"
        title: window.tr("Подтвердить изменения", "Approve changes")
        modal: true
        standardButtons: Dialog.Yes | Dialog.No
        width: Math.min(window.width * 0.72, 900)
        height: Math.min(window.height * 0.72, 680)
        anchors.centerIn: parent
        onAccepted: bridge.answerApproval(true)
        onRejected: bridge.answerApproval(false)
        onOpened: {
            const applyButton = standardButton(Dialog.Yes)
            const rejectButton = standardButton(Dialog.No)
            if (applyButton)
                applyButton.text = window.tr("Применить", "Apply")
            if (rejectButton)
                rejectButton.text = window.tr("Не применять", "Don't apply")
        }
        background: Rectangle { radius: themeTokens.radius
            color: themeTokens.plate
            border.color: themeTokens.rule }
        contentItem: ColumnLayout {
            spacing: 8 * window.densityScale
            Text {
                        font.family: themeTokens.text
                        text: window.tr(
                            "Агент предлагает применить diff к файлам выбранного проекта.",
                            "The agent requests applying this diff to files in the selected project."
                        )
                color: inkSoft
                font.pixelSize: Math.round(11 * window.textScale)
                wrapMode: Text.WordWrap
                Layout.fillWidth: true }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                TextArea {
                    text: approvalPreview.text
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.NoWrap
                    color: ink
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(10.5 * window.textScale)
                    background: Rectangle { radius: themeTokens.radius
                        color: themeTokens.plate }
                }
            }
        }
    }
    Popup {
        id: modelPickerPopup
        objectName: "modelPickerPopup"
        x: {
            const point = modelSelectorButton.mapToItem(window.contentItem, 0, modelSelectorButton.height)
            return Math.max(12, Math.min(window.width - width - 12, point.x))
        }
        y: modelSelectorButton.mapToItem(window.contentItem, 0, modelSelectorButton.height).y + 6
        width: 276
        height: Math.min(window.height - 70, Math.max(
            184,
            (window.modelPickerExpanded ? bridge.modelChoices.length : window.modelPickerPrimaryChoices.length)
                * 38 + 150
        ))
        padding: 10
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onClosed: window.modelPickerExpanded = false
        background: Rectangle {
            radius: themeTokens.radius
            color: themeTokens.light ? "#FCFFFFFF"
                : themeTokens.graphiteTheme ? "#F51C2127" : "#F009211A"
            border.color: themeTokens.lineStrong
            border.width: 1
        }
        contentItem: ColumnLayout {
            spacing: 6
            Text {
                text: window.tr("РАБОЧАЯ МОДЕЛЬ", "WORKING MODEL")
                color: themeTokens.ink3
                font.family: themeTokens.mono
                font.pixelSize: Math.round(8.5 * window.textScale)
                font.letterSpacing: 1.1
            }
            Text {
                text: window.tr("Выбрана · ", "Selected · ") + bridge.modelName
                color: themeTokens.ink4
                font.family: themeTokens.mono
                font.pixelSize: Math.round(9 * window.textScale)
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            Text {
                objectName: "modelCatalogStatus"
                text: {
                    if (bridge.modelCatalogBusy)
                        return window.tr("Загрузка моделей из API…", "Loading models from API…")
                    let status = bridge.modelCatalogSource === "live"
                        ? window.tr("API · ", "API · ") + bridge.modelChoices.length
                            + window.tr(" моделей", " models")
                        : bridge.modelCatalogSource === "cache"
                            ? window.tr("Кэш API · ", "API cache · ") + bridge.modelChoices.length
                                + window.tr(" моделей", " models")
                            : window.tr("Модели из настроек", "Configured models")
                    if (bridge.modelCatalogError)
                        status += " · " + bridge.modelCatalogError
                    return status
                }
                color: bridge.modelCatalogError ? themeTokens.warn : themeTokens.ink3
                font.family: themeTokens.text
                font.pixelSize: Math.round(8.5 * window.textScale)
                Layout.fillWidth: true
                Layout.maximumHeight: 34
                maximumLineCount: 2
                wrapMode: Text.WordWrap
                elide: Text.ElideRight
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                ColumnLayout {
                    width: parent.availableWidth
                    spacing: 3
                    Repeater {
                        model: window.modelPickerChoices
                        delegate: Button {
                            required property string modelData
                            objectName: "modelChoice_" + modelData
                            Layout.fillWidth: true
                            Layout.preferredHeight: 34
                            onClicked: {
                                bridge.setModel(modelData)
                                modelPickerPopup.close()
                            }
                            background: Rectangle {
                                radius: 8
                                color: bridge.modelName === modelData ? themeTokens.primaryTint
                                    : parent.hovered ? themeTokens.surface2 : "transparent"
                                border.color: bridge.modelName === modelData ? themeTokens.lineStrong : "transparent"
                            }
                            contentItem: RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 9
                                anchors.rightMargin: 9
                                spacing: 7
                                Text {
                                    text: modelData
                                    color: themeTokens.ink2
                                    font.family: themeTokens.mono
                                    font.pixelSize: Math.round(9.5 * window.textScale)
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: bridge.modelName === modelData ? "✓" : ""
                                    color: themeTokens.primaryDeep
                                    font.pixelSize: Math.round(11 * window.textScale)
                                    font.bold: true
                                }
                            }
                        }
                    }
                }
            }
            Button {
                objectName: "expandModelCatalogButton"
                visible: window.modelPickerAdditionalChoices.length > 0
                Layout.fillWidth: true
                Layout.preferredHeight: 28
                text: window.modelPickerExpanded
                    ? window.tr("Скрыть остальные модели", "Hide additional models")
                    : window.tr("Ещё модели · ", "More models · ")
                        + window.modelPickerAdditionalChoices.length
                onClicked: window.modelPickerExpanded = !window.modelPickerExpanded
                background: Rectangle {
                    radius: themeTokens.radiusControl
                    color: parent.hovered ? themeTokens.surface3 : "transparent"
                    border.color: themeTokens.line
                }
                contentItem: Text {
                    text: parent.text
                    color: themeTokens.primaryDeep
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(9 * window.textScale)
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
            Button {
                objectName: "refreshModelCatalogButton"
                Layout.fillWidth: true
                Layout.preferredHeight: 28
                text: bridge.credentialBusy
                    ? window.tr("Сохраняю подключение…", "Saving credentials…")
                    : bridge.modelCatalogBusy
                    ? window.tr("Обновление…", "Refreshing…")
                    : window.tr("Обновить список моделей", "Refresh model list")
                enabled: !bridge.modelCatalogBusy && !bridge.credentialBusy
                onClicked: bridge.refreshModels()
                background: Rectangle {
                    radius: themeTokens.radiusControl
                    color: parent.enabled ? themeTokens.surface2 : themeTokens.surface
                    border.color: themeTokens.line
                }
                contentItem: Text {
                    text: parent.text
                    color: themeTokens.ink2
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(9 * window.textScale)
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }
    }

    Dialog {
        id: fileDiffDialog
        objectName: "fileDiffDialog"
        title: window.tr("Изменения · ", "Changes · ") + bridge.openPath
        modal: true
        standardButtons: Dialog.Close
        width: Math.min(window.width * 0.76, 980)
        height: Math.min(window.height * 0.76, 720)
        anchors.centerIn: parent
        background: Rectangle { radius: themeTokens.radius; color: themeTokens.plate; border.color: themeTokens.rule }
        contentItem: ColumnLayout {
            spacing: 8 * window.densityScale
            Text {
                text: window.tr(
                    "Сравнение с версией файла на момент открытия. Diff включает несохранённые и сохранённые изменения этой сессии.",
                    "Compared with the file version from when it was opened. Diff includes unsaved and saved changes from this session."
                )
                color: inkSoft
                font.pixelSize: Math.round(11 * window.textScale)
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                TextArea {
                    id: fileDiffPreview
                    objectName: "fileDiffPreview"
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.NoWrap
                    textFormat: TextEdit.RichText
                    color: ink
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(11 * window.textScale)
                    background: Rectangle { radius: themeTokens.radius; color: themeTokens.plate }
                }
            }
        }
    }
    FileDialog {
        id: attachmentFileDialog
        objectName: "attachmentFileDialog"
        title: window.tr("Прикрепить файл к сообщению", "Attach a file to the message")
        fileMode: FileDialog.OpenFile
        nameFilters: [
            window.tr("Текст, код и данные", "Text, source, and data files")
                + " (*.txt *.md *.py *.json *.yaml *.yml *.toml *.xml *.csv *.js *.ts *.html *.css)",
            window.tr("Все файлы", "All files") + " (*)"
        ]
        onAccepted: bridge.attachFile(selectedFile.toString())
    }

    FolderDialog {
        id: projectFolderDialog
        objectName: "projectFolderDialog"
        title: window.tr(
            "Откройте или создайте папку проекта",
            "Open or create a project folder"
        )
        onAccepted: bridge.selectProjectFolder(selectedFolder.toString(), editor.text)
    }
    Text {
                        font.family: themeTokens.text
                        id: approvalPreview
        visible: false
        text: "" }

    Dialog {
        id: newFileDialog
        objectName: "newFileDialog"
        title: window.tr("Создать файл проекта", "Create project file")
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        anchors.centerIn: parent
        width: 430
        onAccepted: {
            bridge.requestCreateFile(newFilePath.text, editor.text)
            newFilePath.clear()
        }
        contentItem: ColumnLayout {
            spacing: 8 * window.densityScale
            Text { text:window.tr("Путь относительно корня выбранного проекта", "Path relative to the selected project root"); color: inkSoft; font.pixelSize: Math.round(11 * window.textScale) }
            TextField {
                id: newFilePath
                objectName: "newFilePath"
                Layout.fillWidth: true
                placeholderText: window.tr("например src/new_file.py", "for example src/new_file.py")
                selectByMouse: true
            }
        }
    }

    Connections {
        target: bridge
        function onApprovalRequested(diff) {
            approvalPreview.text = diff
            approvalDialog.open()
        }
        function onCredentialSaved(_result) { credential.clear() }
        function onEditorChanged() { editor.text = bridge.editorContents }
    }

    Timer {
        interval: 1500
        running: true
        repeat: true
        onTriggered: bridge.refresh()
    }
}
