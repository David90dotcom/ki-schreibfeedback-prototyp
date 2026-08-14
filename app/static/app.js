(() => {
    "use strict";

    const CUSTOM_MODEL_VALUE = "__custom__";
    const SLOW_RESPONSE_DELAY_MS = 8000;
    const RUNPOD_STATUS_POLL_INTERVAL_MS = 3000;
    const RUNPOD_JOBS_REQUEST_TIMEOUT_MS = 8000;

    const form = document.querySelector("#analysis-form");

    if (!form) {
        return;
    }

    const taskSelect = form.querySelector("#task-id");
    const advancedOptionsToggle = form.querySelector(
        "[data-advanced-options-toggle]"
    );
    const advancedOptionSections = form.querySelectorAll(
        "[data-advanced-options]"
    );
    const standardOptionSections = form.querySelectorAll(
        "[data-standard-options]"
    );
    const contextFreeOption = form.querySelector(
        "[data-context-free-option]"
    );
    const taskPreviews = form.querySelectorAll(
        "[data-task-preview]"
    );
    const analysisOriginalTextPanel = form.querySelector(
        "[data-analysis-original-text-panel]"
    );
    const originalTextInput = form.querySelector(
        "#original-text"
    );
    const analysisPipelineOption = form.querySelector(
        "[data-analysis-pipeline-option]"
    );
    const rubricAnalysisModeInputs = form.querySelectorAll(
        "[data-rubric-analysis-mode]"
    );
    const providerSelect = form.querySelector("#provider");
    const providerPanels = form.querySelectorAll(
        "[data-provider-panel]"
    );
    const modelSelects = form.querySelectorAll(
        "[data-model-select]"
    );
    const ollamaBaseUrlInput = form.querySelector(
        "#ollama-base-url"
    );
    const ollamaModelSelect = form.querySelector(
        "#ollama-model"
    );
    const loadOllamaModelsButton = form.querySelector(
        "#load-ollama-models"
    );
    const ollamaStatus = form.querySelector(
        "#ollama-model-status"
    );
    const runpodEndpointSelect = form.querySelector(
        "#runpod-endpoint"
    );
    const runpodReadiness = form.querySelector(
        "#runpod-readiness"
    );
    const refreshRunpodStatusButton = form.querySelector(
        "#refresh-runpod-status"
    );
    const runpodWorkerStatus = form.querySelector(
        "#runpod-worker-status"
    );
    const runpodSupplyStatus = form.querySelector(
        "#runpod-supply-status"
    );
    const runpodWarmWindow = form.querySelector(
        "#runpod-warm-window"
    );
    const runpodStatusTime = form.querySelector(
        "#runpod-status-time"
    );
    const runpodAggregateStatus = form.querySelector(
        "#runpod-aggregate-status"
    );
    const runpodTrackingInput = form.querySelector(
        "#runpod-tracking-id"
    );
    const runpodJobManager = form.querySelector(
        "#runpod-job-manager"
    );
    const runpodActiveJobs = form.querySelector(
        "#runpod-active-jobs"
    );
    const manualRunpodJobId = form.querySelector(
        "#manual-runpod-job-id"
    );
    const cancelManualRunpodJobButton = form.querySelector(
        "#cancel-manual-runpod-job"
    );
    const runpodJobManagerStatus = form.querySelector(
        "#runpod-job-manager-status"
    );
    const runpodStatusNote = form.querySelector(
        "#runpod-status-note"
    );
    const runpodWorkerDetails = form.querySelector(
        "#runpod-worker-details"
    );
    const submitButton = form.querySelector(
        "#submit-button"
    );
    const loadingIndicator = form.querySelector(
        "#analysis-loading"
    );
    const loadingMessage = form.querySelector(
        "#loading-message"
    );
    const loadingHint = form.querySelector(
        "#loading-hint"
    );
    const cancelCurrentRunpodJobButton = form.querySelector(
        "#cancel-current-runpod-job"
    );
    const analysisResponse = document.querySelector(
        "#analysis-response"
    );

    let slowResponseTimer = null;
    let elapsedTimer = null;
    let runpodPollingTimer = null;
    let loadingStartedAt = null;
    let liveMessage = "";
    let liveHint = "";
    let latestRunpodSnapshot = null;
    let activeRunpodTrackingId = null;
    let currentRunpodJobId = null;
    let currentRunpodJobStatus = null;
    let runpodJobsRequestInFlight = false;
    let criterionRefreshInFlight = false;
    const runpodStatusRequestsInFlight = new Set();

    function advancedOptionsEnabled() {
        return Boolean(advancedOptionsToggle?.checked);
    }

    function selectedRubricAnalysisMode() {
        return (
            [...rubricAnalysisModeInputs].find(
                (input) => input.checked
            )?.value || (taskSelect?.value ? "criterion_wise" : "joint")
        );
    }

    function selectedCriterionCount() {
        const rawCount = Number(
            taskSelect?.selectedOptions?.[0]?.dataset
                .criterionCount
        );

        return Number.isInteger(rawCount) && rawCount > 0
            ? rawCount
            : null;
    }

    function updatePipelineOptionAvailability() {
        const taskSelected = Boolean(taskSelect?.value);
        const advancedOptions = advancedOptionsEnabled();

        if (analysisPipelineOption) {
            analysisPipelineOption.hidden =
                !taskSelected || !advancedOptions;
            analysisPipelineOption.classList.remove(
                "analysis-pipeline-option-unavailable"
            );
        }

        rubricAnalysisModeInputs.forEach((input) => {
            input.disabled = !taskSelected || !advancedOptions;

            if (
                taskSelected &&
                !advancedOptions &&
                input.value === "criterion_wise"
            ) {
                input.checked = true;
            }
        });
    }

    function updateAdvancedOptionsVisibility() {
        const advancedOptions = advancedOptionsEnabled();
        const selectedProvider = providerSelect?.value;

        if (contextFreeOption) {
            contextFreeOption.disabled = !advancedOptions;
            contextFreeOption.textContent = advancedOptions
                ? "Ohne Feedback-Vorlage – bisheriges Gesamtfeedback"
                : "Bitte Aufgabe mit Feedback-Vorlage auswählen";
        }

        if (taskSelect) {
            taskSelect.required = !advancedOptions;
        }

        if (!advancedOptions && taskSelect && !taskSelect.value) {
            const defaultTaskId = taskSelect.dataset.defaultTaskId;

            if (
                defaultTaskId &&
                [...taskSelect.options].some(
                    (option) => option.value === defaultTaskId
                )
            ) {
                taskSelect.value = defaultTaskId;
            }
        }

        form
            .querySelectorAll("[data-advanced-model-option]")
            .forEach((option) => {
                option.hidden = !advancedOptions;
                option.disabled = !advancedOptions;
            });

        if (!advancedOptions) {
            modelSelects.forEach((modelSelect) => {
                if (
                    modelSelect.selectedOptions[0]?.matches(
                        "[data-advanced-model-option]"
                    )
                ) {
                    modelSelect.value =
                        modelSelect.dataset.defaultModel || "";
                }
            });
        }

        advancedOptionSections.forEach((section) => {
            section.hidden = !advancedOptions;
            const providerPanel = section.closest(
                "[data-provider-panel]"
            );
            const providerPanelActive =
                !providerPanel ||
                providerPanel.dataset.providerPanel === selectedProvider;

            section
                .querySelectorAll("input, select, button")
                .forEach((control) => {
                    control.disabled =
                        !advancedOptions || !providerPanelActive;
                });
        });

        standardOptionSections.forEach((section) => {
            section.hidden = advancedOptions;
        });

        modelSelects.forEach(updateCustomModelField);
        updatePipelineOptionAvailability();

        if (ollamaBaseUrlInput) {
            ollamaBaseUrlInput.required =
                advancedOptions && selectedProvider === "ollama";
        }
    }

    function updateTaskPreview() {
        if (!taskSelect) {
            return;
        }

        const taskSelected = Boolean(taskSelect.value);

        taskPreviews.forEach((preview) => {
            const selected =
                preview.dataset.taskPreview === taskSelect.value;

            preview.hidden = !selected;

            if (!selected) {
                preview.open = false;
            }
        });

        if (analysisOriginalTextPanel) {
            analysisOriginalTextPanel.hidden = !taskSelected;

            if (!taskSelected) {
                analysisOriginalTextPanel.open = false;
            }
        }

        if (originalTextInput) {
            originalTextInput.disabled = !taskSelected;
        }

        updatePipelineOptionAvailability();
    }

    function updateCustomModelField(modelSelect) {
        const panel = modelSelect.closest(
            "[data-provider-panel]"
        );
        const customGroup = panel?.querySelector(
            "[data-custom-model-group]"
        );
        const customInput = panel?.querySelector(
            "[data-custom-model-input]"
        );

        if (!panel || !customGroup || !customInput) {
            return;
        }

        const customSelected =
            modelSelect.value === CUSTOM_MODEL_VALUE;
        const panelActive = !panel.hidden;
        const advancedOptions = advancedOptionsEnabled();

        customGroup.hidden =
            !customSelected || !advancedOptions;
        customInput.disabled =
            !customSelected || !panelActive || !advancedOptions;
        customInput.required =
            customSelected && panelActive && advancedOptions;
    }

    function updateProviderPanels() {
        const selectedProvider = providerSelect.value;

        providerPanels.forEach((panel) => {
            const panelActive =
                panel.dataset.providerPanel === selectedProvider;

            panel.hidden = !panelActive;

            panel
                .querySelectorAll("input, select, button")
                .forEach((control) => {
                    control.disabled = !panelActive;
                });

            const modelSelect = panel.querySelector(
                "[data-model-select]"
            );

            if (modelSelect) {
                updateCustomModelField(modelSelect);
            }
        });

        if (selectedProvider === "runpod") {
            void loadRunpodStatus();

            if (advancedOptionsEnabled()) {
                void loadRunpodJobs();
            }
        }

        updateAdvancedOptionsVisibility();
        updatePipelineOptionAvailability();
    }

    function setOllamaStatus(message, status) {
        if (!ollamaStatus) {
            return;
        }

        ollamaStatus.textContent = message;
        ollamaStatus.className = "status-message";

        if (status) {
            ollamaStatus.classList.add(`status-${status}`);
        }
    }

    function addModelOption(modelName, label, advanced = false) {
        const option = document.createElement("option");
        option.value = modelName;
        option.textContent = label;

        if (advanced) {
            option.dataset.advancedModelOption = "";
        }

        ollamaModelSelect.append(option);
    }

    function replaceOllamaModelOptions(
        modelNames,
        defaultModel
    ) {
        const currentSelection = ollamaModelSelect.value;
        const discoveredModels = [...new Set(modelNames)]
            .filter(
                (modelName) =>
                    typeof modelName === "string" &&
                    modelName.trim()
            )
            .sort((left, right) =>
                left.localeCompare(right)
            );

        ollamaModelSelect.replaceChildren();

        const defaultDiscovered =
            discoveredModels.includes(defaultModel);
        const defaultLabel = defaultDiscovered
            ? `${defaultModel} (Standard aus .env)`
            : `${defaultModel} (Standard aus .env – nicht von Ollama gemeldet)`;

        addModelOption(defaultModel, defaultLabel);

        discoveredModels.forEach((modelName) => {
            if (modelName !== defaultModel) {
                addModelOption(modelName, modelName, true);
            }
        });

        addModelOption(
            CUSTOM_MODEL_VALUE,
            "Andere Modell-ID …",
            true
        );

        if (currentSelection === CUSTOM_MODEL_VALUE) {
            ollamaModelSelect.value = CUSTOM_MODEL_VALUE;
        } else if (
            currentSelection === defaultModel ||
            discoveredModels.includes(currentSelection)
        ) {
            ollamaModelSelect.value = currentSelection;
        } else {
            ollamaModelSelect.value = defaultModel;
        }

        updateAdvancedOptionsVisibility();
        updateCustomModelField(ollamaModelSelect);
    }

    async function loadOllamaModels() {
        const baseUrl = ollamaBaseUrlInput?.value.trim();

        setOllamaStatus(
            "Verbindung zu Ollama wird geprüft …",
            null
        );
        loadOllamaModelsButton.disabled = true;

        try {
            const parameters = new URLSearchParams();

            if (baseUrl) {
                parameters.set("base_url", baseUrl);
            }

            const response = await fetch(
                `/api/ollama/models?${parameters}`,
                {
                    headers: {Accept: "application/json"},
                }
            );
            const payload = await response.json();

            if (!response.ok) {
                const message =
                    payload?.detail?.message ||
                    payload?.detail ||
                    "Die Ollama-Modellliste konnte nicht geladen werden.";

                throw new Error(message);
            }

            if (!Array.isArray(payload.models)) {
                throw new Error(
                    "Ollama hat keine gültige Modellliste zurückgegeben."
                );
            }

            if (ollamaBaseUrlInput) {
                ollamaBaseUrlInput.value = payload.base_url;
            }

            replaceOllamaModelOptions(
                payload.models,
                payload.default_model
            );

            const message = payload.models.length
                ? payload.message
                : "Ollama ist erreichbar, meldet aber keine installierten Modelle.";

            setOllamaStatus(
                message,
                payload.models.length ? "success" : "warning"
            );
        } catch (error) {
            const message =
                error instanceof Error
                    ? error.message
                    : "Die Ollama-Modellliste konnte nicht geladen werden.";

            setOllamaStatus(message, "error");
        } finally {
            loadOllamaModelsButton.disabled =
                providerSelect.value !== "ollama";
        }
    }

    function setStatusBadge(element, label, tone) {
        if (!element) {
            return;
        }

        element.textContent = label;
        element.className =
            `status-badge status-${tone || "neutral"}`;
    }

    function formatLocalTime(value, includeSeconds = false) {
        const timestamp = new Date(value);

        if (Number.isNaN(timestamp.getTime())) {
            return "–";
        }

        return timestamp.toLocaleTimeString("de-DE", {
            hour: "2-digit",
            minute: "2-digit",
            second: includeSeconds ? "2-digit" : undefined,
        });
    }

    function formatElapsed(milliseconds) {
        const totalSeconds = Math.max(
            0,
            Math.floor(milliseconds / 1000)
        );
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;

        return `${String(minutes).padStart(2, "0")}:${String(
            seconds
        ).padStart(2, "0")}`;
    }

    function formatUptime(seconds) {
        if (!Number.isInteger(seconds) || seconds < 0) {
            return "unbekannt";
        }

        if (seconds < 60) {
            return `${seconds} s`;
        }

        const minutes = Math.floor(seconds / 60);
        return `${minutes} min`;
    }

    function addTextElement(parent, tagName, text, className) {
        const element = document.createElement(tagName);
        element.textContent = text;

        if (className) {
            element.className = className;
        }

        parent.append(element);
        return element;
    }

    function generateTrackingId() {
        if (typeof window.crypto?.randomUUID === "function") {
            return window.crypto.randomUUID();
        }

        const bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        const hex = [...bytes]
            .map((value) => value.toString(16).padStart(2, "0"))
            .join("");

        return [
            hex.slice(0, 8),
            hex.slice(8, 12),
            hex.slice(12, 16),
            hex.slice(16, 20),
            hex.slice(20),
        ].join("-");
    }

    function formatJobAge(seconds) {
        if (!Number.isInteger(seconds) || seconds < 0) {
            return "Dauer unbekannt";
        }

        if (seconds < 60) {
            return `${seconds} s`;
        }

        const minutes = Math.floor(seconds / 60);

        if (minutes < 60) {
            return `${minutes} min`;
        }

        const hours = Math.floor(minutes / 60);
        const remainingMinutes = minutes % 60;
        return remainingMinutes
            ? `${hours} h ${remainingMinutes} min`
            : `${hours} h`;
    }

    function runpodJobStatusLabel(status) {
        const labels = {
            IN_QUEUE: "Wartet in Queue",
            IN_PROGRESS: "Wird verarbeitet",
            RUNNING: "Wird verarbeitet",
        };

        return labels[status] || status || "Status unbekannt";
    }

    function setRunpodJobManagerStatus(message, tone) {
        if (!runpodJobManagerStatus) {
            return;
        }

        runpodJobManagerStatus.textContent = message;
        runpodJobManagerStatus.className = "status-message";

        if (tone) {
            runpodJobManagerStatus.classList.add(`status-${tone}`);
        }
    }

    function setRunpodLoadingTone(tone) {
        loadingIndicator.classList.remove(
            "loading-neutral",
            "loading-warning",
            "loading-success",
            "loading-error"
        );
        loadingIndicator.classList.add(`loading-${tone}`);
    }

    function updateLiveMessageFromJob(job) {
        if (!job) {
            return;
        }

        if (job.status === "IN_QUEUE") {
            setRunpodLoadingTone("warning");
            setLiveMessage(
                "Deine Anfrage wartet in der RunPod-Warteschlange",
                `Request-ID ${job.jobId} · bisher ${formatJobAge(
                    job.ageSeconds
                )}`
            );
            return;
        }

        if (["IN_PROGRESS", "RUNNING"].includes(job.status)) {
            setRunpodLoadingTone("success");
            setLiveMessage(
                "Ein Worker verarbeitet jetzt deine Anfrage",
                `Request-ID ${job.jobId} · Abbruch bleibt bis zum Abschluss möglich.`
            );
        }
    }

    function renderActiveRunpodJobs(jobs) {
        if (!runpodActiveJobs) {
            return;
        }

        runpodActiveJobs.replaceChildren();

        if (!jobs.length) {
            addTextElement(
                runpodActiveJobs,
                "p",
                "Keine aktiven, von dieser Web-App registrierten Anfragen.",
                "hint"
            );
            return;
        }

        const list = document.createElement("ul");
        list.className = "runpod-job-list";

        jobs.forEach((job) => {
            const item = document.createElement("li");
            const information = document.createElement("div");
            information.className = "runpod-job-information";
            addTextElement(
                information,
                "code",
                job.jobId,
                "runpod-job-id"
            );
            addTextElement(
                information,
                "span",
                `${runpodJobStatusLabel(job.status)} · ${formatJobAge(
                    job.ageSeconds
                )}${job.statusFresh ? "" : " · letzter bekannter Stand"}`,
                "runpod-job-meta"
            );

            const cancelButton = document.createElement("button");
            cancelButton.type = "button";
            cancelButton.className =
                "danger-button compact-button runpod-job-cancel";
            cancelButton.textContent = "Abbrechen";
            cancelButton.addEventListener("click", () => {
                void cancelRunpodJob(job.jobId, cancelButton);
            });

            item.append(information, cancelButton);
            list.append(item);
        });

        runpodActiveJobs.append(list);
    }

    async function loadRunpodJobs({live = false} = {}) {
        if (
            !runpodJobManager ||
            !runpodEndpointSelect ||
            providerSelect.value !== "runpod" ||
            runpodJobsRequestInFlight
        ) {
            return null;
        }

        const jobsUrl = runpodJobManager.dataset.jobsUrl;
        const selectedEndpoint = runpodEndpointSelect.value;

        if (!jobsUrl || !selectedEndpoint) {
            return null;
        }

        runpodJobsRequestInFlight = true;
        const controller = new AbortController();
        const timeoutId = window.setTimeout(
            () => controller.abort(),
            RUNPOD_JOBS_REQUEST_TIMEOUT_MS
        );

        try {
            const parameters = new URLSearchParams({
                endpoint_key: selectedEndpoint,
            });
            const response = await fetch(
                `${jobsUrl}?${parameters}`,
                {
                    headers: {Accept: "application/json"},
                    signal: controller.signal,
                }
            );
            const payload = await response.json();

            if (response.status === 401) {
                window.location.assign("/login");
                return null;
            }

            if (!response.ok) {
                throw new Error(
                    payload?.detail?.message ||
                        "Aktive RunPod-Anfragen konnten nicht geladen werden."
                );
            }

            if (runpodEndpointSelect.value !== selectedEndpoint) {
                return null;
            }

            const jobs = Array.isArray(payload.jobs)
                ? payload.jobs
                : [];
            renderActiveRunpodJobs(jobs);

            const currentJob = activeRunpodTrackingId
                ? jobs.find(
                    (job) =>
                        job.trackingId === activeRunpodTrackingId
                )
                : null;

            if (currentJob) {
                currentRunpodJobId = currentJob.jobId;
                currentRunpodJobStatus = currentJob.status;

                if (cancelCurrentRunpodJobButton) {
                    cancelCurrentRunpodJobButton.hidden = false;
                }

                if (live) {
                    updateLiveMessageFromJob(currentJob);
                }
            } else if (!activeRunpodTrackingId) {
                currentRunpodJobId = null;
                currentRunpodJobStatus = null;

                if (cancelCurrentRunpodJobButton) {
                    cancelCurrentRunpodJobButton.hidden = true;
                }
            }

            return payload;
        } catch (error) {
            const requestTimedOut =
                error instanceof DOMException &&
                error.name === "AbortError";
            const message = requestTimedOut
                ? "Die gespeicherten Anfragen konnten nicht rechtzeitig geladen werden. Bitte erneut versuchen."
                : error instanceof Error
                    ? error.message
                    : "Aktive RunPod-Anfragen konnten nicht geladen werden.";
            runpodActiveJobs.replaceChildren();
            addTextElement(
                runpodActiveJobs,
                "p",
                message,
                "hint status-error"
            );
            setRunpodJobManagerStatus(message, "error");
            return null;
        } finally {
            window.clearTimeout(timeoutId);
            runpodJobsRequestInFlight = false;
        }
    }

    async function cancelRunpodJob(jobId, triggerButton = null) {
        if (!runpodJobManager || !runpodEndpointSelect || !jobId) {
            return;
        }

        const confirmed = window.confirm(
            `Soll genau diese RunPod-Anfrage abgebrochen werden?\n\n${jobId}`
        );

        if (!confirmed) {
            return;
        }

        const cancelUrl = runpodJobManager.dataset.cancelUrl;
        const csrfToken = form.querySelector(
            'input[name="csrf_token"]'
        )?.value;

        if (!cancelUrl || !csrfToken) {
            setRunpodJobManagerStatus(
                "Die Anfrage kann momentan nicht bestätigt werden.",
                "error"
            );
            return;
        }

        if (triggerButton) {
            triggerButton.disabled = true;
        }

        setRunpodJobManagerStatus(
            "RunPod-Anfrage wird abgebrochen …",
            null
        );

        try {
            const formData = new FormData();
            formData.set("endpoint_key", runpodEndpointSelect.value);
            formData.set("job_id", jobId);
            formData.set("csrf_token", csrfToken);

            const response = await fetch(cancelUrl, {
                method: "POST",
                body: formData,
                headers: {Accept: "application/json"},
            });
            const payload = await response.json();

            if (response.status === 401) {
                window.location.assign("/login");
                return;
            }

            if (!response.ok) {
                throw new Error(
                    payload?.detail?.message ||
                        "Die RunPod-Anfrage konnte nicht abgebrochen werden."
                );
            }

            setRunpodJobManagerStatus(payload.message, "success");

            if (jobId === currentRunpodJobId) {
                setRunpodLoadingTone("error");
                setLiveMessage(
                    "Deine RunPod-Anfrage wurde abgebrochen",
                    `Request-ID ${jobId}`
                );
                currentRunpodJobStatus = "CANCELLED";
                cancelCurrentRunpodJobButton.hidden = true;
            }

            if (manualRunpodJobId?.value.trim() === jobId) {
                manualRunpodJobId.value = "";
            }

            await loadRunpodJobs();
            void loadRunpodStatus();
        } catch (error) {
            const message =
                error instanceof Error
                    ? error.message
                    : "Die RunPod-Anfrage konnte nicht abgebrochen werden.";
            setRunpodJobManagerStatus(message, "error");
        } finally {
            if (triggerButton) {
                triggerButton.disabled = false;
            }
        }
    }

    function formatWorkerCounts(counts) {
        const safeCounts = counts || {};
        const values = [
            ["idle", "idle"],
            ["ready", "bereit"],
            ["running", "laufend"],
            ["initializing", "startend"],
            ["throttled", "gedrosselt"],
            ["unhealthy", "fehlerhaft"],
        ];

        return values
            .map(([key, label]) => {
                const value = Number.isInteger(safeCounts[key])
                    ? safeCounts[key]
                    : 0;
                return `${label}: ${value}`;
            })
            .join(" · ");
    }

    function renderAggregateStatus(counts) {
        if (!runpodAggregateStatus) {
            return;
        }

        runpodAggregateStatus
            .querySelectorAll("[data-worker-count]")
            .forEach((field) => {
                const value = counts?.[field.dataset.workerCount];
                field.textContent = Number.isInteger(value)
                    ? String(value)
                    : "–";
            });
    }

    function hasAggregateWorkers(counts) {
        return Object.values(counts || {}).some(
            (value) => Number.isInteger(value) && value > 0
        );
    }

    function renderWorkerDetails(technical, configuration) {
        if (!runpodWorkerDetails) {
            return;
        }

        runpodWorkerDetails.replaceChildren();

        if (configuration?.available) {
            const idleLabel = Number.isInteger(
                configuration.idleTimeoutSeconds
            )
                ? `${Math.round(
                    configuration.idleTimeoutSeconds / 60
                )} min Idle-Timeout`
                : "Idle-Timeout unbekannt";
            const executionLabel = Number.isInteger(
                configuration.executionTimeoutMs
            )
                ? `${Math.round(
                    configuration.executionTimeoutMs / 1000
                )} s Ausführungslimit`
                : "Ausführungslimit unbekannt";
            const workerRange =
                configuration.minimumWorkers !== null &&
                configuration.maximumWorkers !== null
                    ? `${configuration.minimumWorkers}–${configuration.maximumWorkers} Worker`
                    : "Workerlimit unbekannt";
            const flashboot = configuration.flashboot
                ? `FlashBoot ${configuration.flashboot}`
                : "FlashBoot unbekannt";
            const pools = Array.isArray(configuration.gpuPools) &&
                configuration.gpuPools.length
                ? `GPU-Pool ${configuration.gpuPools.join(", ")}`
                : Array.isArray(configuration.gpuTypeIds) &&
                    configuration.gpuTypeIds.length
                    ? `GPU-Typ ${configuration.gpuTypeIds.join(", ")}`
                    : "GPU-Pool unbekannt";

            addTextElement(
                runpodWorkerDetails,
                "p",
                `Endpoint: ${pools} · ${idleLabel} · ${executionLabel} · ${workerRange} · ${flashboot}`,
                "technical-summary"
            );

            if (
                configuration.source !== "rest_v2" &&
                configuration.message
            ) {
                addTextElement(
                    runpodWorkerDetails,
                    "p",
                    configuration.message,
                    "technical-summary"
                );
            }
        } else if (configuration?.message) {
            addTextElement(
                runpodWorkerDetails,
                "p",
                configuration.message,
                "technical-unavailable"
            );
        }

        if (!technical?.available) {
            addTextElement(
                runpodWorkerDetails,
                "p",
                technical?.message ||
                    "Technische Workerdaten sind nicht abrufbar.",
                "technical-unavailable"
            );

            if (technical?.aggregateAvailable) {
                addTextElement(
                    runpodWorkerDetails,
                    "p",
                    `Aggregierter Status: ${formatWorkerCounts(
                        technical.counts
                    )}`,
                    "technical-summary"
                );
            }

            if (technical?.diagnosticMessage) {
                addTextElement(
                    runpodWorkerDetails,
                    "p",
                    `Technische Diagnose: ${technical.diagnosticMessage}`,
                    "technical-unavailable"
                );
            }
            return;
        }

        const workers = Array.isArray(technical.workers)
            ? technical.workers
            : [];

        if (technical.endpointVersion !== null) {
            addTextElement(
                runpodWorkerDetails,
                "p",
                `Aktueller Endpoint-Release: Version ${technical.endpointVersion}`,
                "technical-summary"
            );
        }

        if (technical.source !== "rest_v2" && technical.message) {
            addTextElement(
                runpodWorkerDetails,
                "p",
                technical.message,
                "technical-summary"
            );
        }

        if (technical.aggregateAvailable) {
            addTextElement(
                runpodWorkerDetails,
                "p",
                `Aggregierter Status: ${formatWorkerCounts(
                    technical.counts
                )}`,
                "technical-summary"
            );
        }

        if (!workers.length) {
            addTextElement(
                runpodWorkerDetails,
                "p",
                hasAggregateWorkers(technical.counts)
                    ? "Aktive Worker sind nur aggregiert gemeldet; eine Einzelzuordnung ist nicht verfügbar."
                    : "Aktuell ist kein aktiver Worker gemeldet.",
                "technical-unavailable"
            );
            return;
        }

        const list = document.createElement("ul");
        list.className = "worker-list";

        workers.forEach((worker) => {
            const item = document.createElement("li");
            const gpu = worker.gpuTypeId || "GPU nicht gemeldet";
            const status = worker.status || "Status unbekannt";
            const workerId = worker.id || "ID unbekannt";
            const version =
                worker.version === null
                    ? "Release unbekannt"
                    : `Release ${worker.version}`;
            const dataCenter =
                worker.dataCenterId || "Rechenzentrum unbekannt";

            addTextElement(item, "strong", gpu);
            addTextElement(
                item,
                "span",
                `${status} · Worker ${workerId}`
            );
            addTextElement(
                item,
                "span",
                `${version} · ${dataCenter} · Laufzeit ${formatUptime(
                    worker.uptimeSeconds
                )}`
            );
            list.append(item);
        });

        runpodWorkerDetails.append(list);
    }

    function renderWarmWindow(snapshot) {
        if (!runpodWarmWindow) {
            return;
        }

        const warmWindow = snapshot?.warmWindow;
        const minutes =
            warmWindow?.idleTimeoutMinutes ||
            Number(runpodReadiness?.dataset.idleTimeoutMinutes) ||
            60;
        const workerState = snapshot?.worker?.state;

        if (
            warmWindow?.estimateActive &&
            !["warm", "processing"].includes(workerState)
        ) {
            runpodWarmWindow.textContent =
                "Warmhalteprognose durch aktuellen Workerstatus nicht bestätigt";
            return;
        }

        if (
            warmWindow?.estimateActive &&
            warmWindow?.estimatedUntil
        ) {
            runpodWarmWindow.textContent =
                `Voraussichtlich bis etwa ${formatLocalTime(
                    warmWindow.estimatedUntil
                )} Uhr warm`;
            return;
        }

        if (snapshot?.worker?.state === "warm") {
            runpodWarmWindow.textContent =
                `Aktiver Worker; nach erfolgreicher Anfrage bis zu ${minutes} Minuten warm`;
            return;
        }

        runpodWarmWindow.textContent =
            `Bis zu ${minutes} Minuten nach der letzten erfolgreichen Anfrage`;
    }

    function renderRunpodSnapshot(snapshot, live = false) {
        latestRunpodSnapshot = snapshot;

        setStatusBadge(
            runpodWorkerStatus,
            snapshot.worker?.label || "Workerstatus nicht abrufbar",
            snapshot.worker?.tone || "neutral"
        );
        setStatusBadge(
            runpodSupplyStatus,
            snapshot.supply?.label || "Nicht abrufbar",
            snapshot.supply?.tone || "neutral"
        );

        if (runpodStatusTime) {
            runpodStatusTime.dateTime = snapshot.checkedAt || "";
            runpodStatusTime.textContent = snapshot.checkedAt
                ? `${formatLocalTime(snapshot.checkedAt, true)} Uhr`
                : "–";
        }

        renderAggregateStatus(snapshot.technical?.counts);

        if (runpodStatusNote) {
            const messages = [
                snapshot.supply?.message ||
                    "Momentaufnahme der allgemeinen RunPod-Kapazität; keine Garantie für einen erfolgreichen Workerstart.",
            ];

            if (
                snapshot.warmWindow?.configurationVerified === false
            ) {
                messages.push(
                    "Das Warmhaltefenster stammt aus der Web-Konfiguration, weil die RunPod-Einstellung nicht lesbar ist."
                );
            }

            runpodStatusNote.textContent = messages.join(" ");
        }

        renderWarmWindow(snapshot);
        renderWorkerDetails(
            snapshot.technical,
            snapshot.configuration
        );
        populateResultTechnical(snapshot);

        if (live) {
            updateLiveRunpodMessage(snapshot);
        }
    }

    function renderRunpodUnavailable(message) {
        setStatusBadge(
            runpodWorkerStatus,
            "Workerstatus nicht abrufbar",
            "neutral"
        );
        setStatusBadge(
            runpodSupplyStatus,
            "Nicht abrufbar",
            "neutral"
        );

        if (runpodStatusNote) {
            runpodStatusNote.textContent = message;
        }

        if (runpodStatusTime) {
            runpodStatusTime.textContent = "–";
        }

        renderAggregateStatus(null);
    }

    async function loadRunpodStatus({live = false} = {}) {
        if (
            !runpodReadiness ||
            !runpodEndpointSelect ||
            providerSelect.value !== "runpod"
        ) {
            return null;
        }

        const selectedEndpoint = runpodEndpointSelect.value;
        const statusUrl = runpodReadiness.dataset.statusUrl;

        if (!statusUrl || !selectedEndpoint) {
            return null;
        }

        if (runpodStatusRequestsInFlight.has(selectedEndpoint)) {
            return null;
        }

        runpodStatusRequestsInFlight.add(selectedEndpoint);

        if (!live) {
            setStatusBadge(
                runpodWorkerStatus,
                "Status wird geladen …",
                "neutral"
            );
            setStatusBadge(
                runpodSupplyStatus,
                "Wird geladen …",
                "neutral"
            );
            renderAggregateStatus(null);
        }

        if (refreshRunpodStatusButton) {
            refreshRunpodStatusButton.disabled = true;
        }

        try {
            const parameters = new URLSearchParams({
                endpoint_key: selectedEndpoint,
            });
            const response = await fetch(
                `${statusUrl}?${parameters}`,
                {
                    headers: {Accept: "application/json"},
                }
            );
            const payload = await response.json();

            if (response.status === 401) {
                window.location.assign("/login");
                return null;
            }

            if (!response.ok) {
                throw new Error(
                    payload?.detail?.message ||
                        "Der RunPod-Status konnte nicht geladen werden."
                );
            }

            if (runpodEndpointSelect.value !== selectedEndpoint) {
                return null;
            }

            renderRunpodSnapshot(payload, live);
            return payload;
        } catch (error) {
            const message =
                error instanceof Error
                    ? error.message
                    : "Der RunPod-Status konnte nicht geladen werden.";

            if (!live) {
                renderRunpodUnavailable(message);
            }

            return null;
        } finally {
            runpodStatusRequestsInFlight.delete(selectedEndpoint);

            if (refreshRunpodStatusButton) {
                refreshRunpodStatusButton.disabled =
                    providerSelect.value !== "runpod" ||
                    runpodStatusRequestsInFlight.size > 0;
            }
        }
    }

    function setLiveMessage(message, hint) {
        liveMessage = message;
        liveHint = hint;
        updateElapsedMessage();
    }

    function updateElapsedMessage() {
        if (loadingStartedAt === null) {
            return;
        }

        const elapsed = formatElapsed(
            Date.now() - loadingStartedAt
        );

        loadingMessage.textContent = `${liveMessage} – ${elapsed}`;
        loadingHint.textContent = liveHint;
    }

    function updateLiveRunpodMessage(snapshot) {
        if (currentRunpodJobStatus === "CANCELLED") {
            return;
        }

        if (
            currentRunpodJobId &&
            ["IN_QUEUE", "IN_PROGRESS", "RUNNING"].includes(
                currentRunpodJobStatus
            )
        ) {
            return;
        }

        const state = snapshot?.worker?.state;
        const jobs = snapshot?.worker?.jobs || {};
        const queuedJobs = Number.isInteger(jobs.inQueue)
            ? jobs.inQueue
            : 0;
        const activeJobs = Number.isInteger(jobs.inProgress)
            ? jobs.inProgress
            : 0;

        if (queuedJobs > 0) {
            setLiveMessage(
                "RunPod meldet eine wartende Anfrage",
                "Der Status gilt für den gesamten Endpoint. Dein Auftrag bleibt aktiv und das Ergebnis erscheint nach Abschluss automatisch."
            );
        } else if (activeJobs > 0 || state === "processing") {
            setLiveMessage(
                "Der Endpoint verarbeitet mindestens eine Anfrage",
                "Der Status gilt für den gesamten Endpoint und belegt noch keine sichere Zuordnung zu deinem Auftrag."
            );
        } else if (state === "initializing") {
            setLiveMessage(
                "Worker wird gestartet – Cold Start läuft",
                "Container, Modell und GPU-Kernels werden vorbereitet. Das kann mehrere Minuten dauern."
            );
        } else if (state === "queued") {
            setLiveMessage(
                "RunPod meldet eine wartende Anfrage",
                "Der Status gilt für den gesamten Endpoint. Das Browserfenster bitte geöffnet lassen."
            );
        } else if (state === "unhealthy") {
            setLiveMessage(
                "Workerstart fehlgeschlagen",
                "RunPod kann automatisch einen Ersatzworker starten. Die Anwendung wartet weiter innerhalb des Zeitlimits."
            );
        } else if (state === "throttled") {
            setLiveMessage(
                "GPU-Kapazität momentan eingeschränkt",
                "Der Auftrag bleibt aktiv; die Bereitstellung kann länger dauern."
            );
        } else if (state === "warm") {
            setLiveMessage(
                "GPU-Kapazität ist verfügbar",
                "Der Auftrag wurde übermittelt; RunPod hat noch keine laufende Jobverarbeitung gemeldet."
            );
        } else if (state === "running") {
            setLiveMessage(
                "Worker läuft – noch keine Jobverarbeitung gemeldet",
                "Ein laufender Worker ist nicht automatisch deinem Auftrag zugeordnet. Der Auftrag bleibt aktiv."
            );
        } else if (state === "cold") {
            setLiveMessage(
                "Auftrag wurde übermittelt – Cold Start erforderlich",
                "RunPod stellt jetzt einen Worker bereit. Das kann mehrere Minuten dauern."
            );
        }
    }

    function startRunpodLiveTracking() {
        loadingStartedAt = Date.now();
        setRunpodLoadingTone("neutral");
        setLiveMessage(
            "Auftrag wird an RunPod übermittelt",
            "Der Status des ausgewählten Endpoints wird regelmäßig geprüft."
        );

        elapsedTimer = window.setInterval(
            updateElapsedMessage,
            1000
        );
        void loadRunpodStatus({live: true});
        void loadRunpodJobs({live: true});
        runpodPollingTimer = window.setInterval(
            () => {
                void loadRunpodStatus({live: true});
                void loadRunpodJobs({live: true});
            },
            RUNPOD_STATUS_POLL_INTERVAL_MS
        );
    }

    function stopRunpodLiveTracking() {
        if (elapsedTimer !== null) {
            window.clearInterval(elapsedTimer);
            elapsedTimer = null;
        }

        if (runpodPollingTimer !== null) {
            window.clearInterval(runpodPollingTimer);
            runpodPollingTimer = null;
        }

        loadingStartedAt = null;
    }

    function beginLoading() {
        form.dataset.submitting = "true";
        form.setAttribute("aria-busy", "true");
        analysisResponse.setAttribute("aria-busy", "true");
        delete analysisResponse.dataset.analysisOutcome;
        submitButton.disabled = true;
        submitButton.textContent = "Feedback wird generiert …";
        loadingIndicator.hidden = false;
        const rubricAnalysisMode = taskSelect?.value
            ? selectedRubricAnalysisMode()
            : "joint";
        const twoPassSelected = rubricAnalysisMode === "two_pass";
        const criterionWiseSelected =
            rubricAnalysisMode === "criterion_wise";
        const criterionCount = selectedCriterionCount();
        const criterionCountLabel = criterionCount
            ? `${criterionCount} Kriterien`
            : "Die Kriterien";

        if (providerSelect.value === "runpod") {
            activeRunpodTrackingId =
                runpodTrackingInput?.value || null;
            currentRunpodJobId = null;
            currentRunpodJobStatus = null;
            startRunpodLiveTracking();

            if (twoPassSelected) {
                setLiveMessage(
                    "RunPod führt zwei aufeinanderfolgende Modellprüfungen aus",
                    "Nach der Befundphase startet mit demselben Modell eine eingeschränkte Zweitprüfung. Dabei können nacheinander zwei Job-IDs entstehen."
                );
            } else if (criterionWiseSelected) {
                setLiveMessage(
                    "RunPod analysiert jedes Kriterium getrennt",
                    `${criterionCountLabel} werden nacheinander als eigenständige Jobs verarbeitet. Die bereits abgeschlossenen Kriterien bleiben voneinander getrennt.`
                );
            }
            return;
        }

        const localModelSelected =
            providerSelect.value === "ollama";

        if (twoPassSelected) {
            loadingMessage.textContent = localModelSelected
                ? "Das lokale Modell führt Befund- und Prüfphase aus …"
                : "Das Cloudmodell führt Befund- und Prüfphase aus …";
            loadingHint.textContent =
                "Der erste Aufruf sammelt belegte Kandidaten. Nach der technischen Prüfung kontrolliert dasselbe Modell nur diese Befunde in einem zweiten Aufruf.";

            slowResponseTimer = window.setTimeout(() => {
                loadingMessage.textContent =
                    "Das experimentelle Zwei-Pass-Feedback arbeitet weiterhin …";
                loadingHint.textContent =
                    "Zwei vollständige Modellaufrufe dauern länger als der bisherige Modus. Bitte lass das Browserfenster bis zum geprüften Ergebnis geöffnet.";
            }, SLOW_RESPONSE_DELAY_MS);
            return;
        }

        if (criterionWiseSelected) {
            loadingMessage.textContent = localModelSelected
                ? "Das lokale Modell analysiert die Kriterien nacheinander …"
                : "Das Cloudmodell analysiert die Kriterien nacheinander …";
            loadingHint.textContent =
                `${criterionCountLabel} erhalten jeweils einen eigenen fokussierten Modellaufruf mit dem vollständigen Schülertext.`;

            slowResponseTimer = window.setTimeout(() => {
                loadingMessage.textContent =
                    "Die kriterienweise Analyse arbeitet weiterhin …";
                loadingHint.textContent =
                    "Jeder Kriterienaufruf wird vollständig abgeschlossen, bevor der nächste beginnt. Größere lokale Modelle können dafür mehrere Minuten benötigen.";
            }, SLOW_RESPONSE_DELAY_MS);
            return;
        }

        loadingMessage.textContent = localModelSelected
            ? "Das lokale Modell verarbeitet den Text …"
            : "Das Cloudmodell verarbeitet den Text …";
        loadingHint.textContent = localModelSelected
            ? "Bitte warten. Der erste Aufruf kann länger dauern, weil das Modell zunächst in den Arbeitsspeicher geladen wird."
            : "Bitte warten. Die Dauer hängt vom ausgewählten Modell und der Verbindung ab.";

        slowResponseTimer = window.setTimeout(() => {
            if (localModelSelected) {
                loadingMessage.textContent =
                    "Das lokale Modell arbeitet weiterhin …";
                loadingHint.textContent =
                    "Das Modell wird möglicherweise gerade in den Arbeitsspeicher geladen. Der erste Aufruf kann deutlich länger dauern.";
            } else {
                loadingMessage.textContent =
                    "Die Cloudanfrage wird weiterhin verarbeitet …";
                loadingHint.textContent =
                    "Bitte lass das Browserfenster geöffnet. Die Antwort wird nach Abschluss automatisch angezeigt.";
            }
        }, SLOW_RESPONSE_DELAY_MS);
    }

    function resetLoadingState() {
        if (slowResponseTimer !== null) {
            window.clearTimeout(slowResponseTimer);
            slowResponseTimer = null;
        }

        stopRunpodLiveTracking();
        delete form.dataset.submitting;
        form.removeAttribute("aria-busy");
        analysisResponse.removeAttribute("aria-busy");
        submitButton.disabled = false;
        submitButton.textContent =
            submitButton.dataset.defaultLabel ||
            "Feedback generieren";
        loadingIndicator.hidden = true;
        loadingIndicator.classList.remove(
            "loading-neutral",
            "loading-warning",
            "loading-success",
            "loading-error"
        );

        activeRunpodTrackingId = null;
        currentRunpodJobId = null;
        currentRunpodJobStatus = null;

        if (cancelCurrentRunpodJobButton) {
            cancelCurrentRunpodJobButton.hidden = true;
        }

        if (runpodTrackingInput) {
            runpodTrackingInput.value = generateTrackingId();
        }
    }

    function renderClientError(message) {
        const section = document.createElement("section");
        section.className = "card error";
        section.dataset.analysisError = "true";
        addTextElement(section, "h2", "Fehler");
        addTextElement(section, "p", message);
        analysisResponse.replaceChildren(section);
        analysisResponse.dataset.analysisOutcome = "error";
        analysisResponse.hidden = false;
        section.tabIndex = -1;
        section.focus({preventScroll: true});
        section.scrollIntoView({
            behavior: "smooth",
            block: "start",
        });
    }

    function renderAnalysisResponse(responseHtml, responseOutcome) {
        const responseTemplate = document.createElement("template");
        responseTemplate.innerHTML = responseHtml.trim();
        const resultOrError = responseTemplate.content.querySelector(
            "[data-analysis-result], [data-analysis-error]"
        );

        if (!resultOrError) {
            console.error(
                "Die Analyseantwort enthielt weder Ergebnis noch Fehler.",
                {
                    responseOutcome,
                    responseLength: responseHtml.length,
                }
            );
            renderClientError(
                "Der Server hat die Anfrage beantwortet, aber weder ein " +
                "darstellbares Ergebnis noch eine Fehlermeldung geliefert. " +
                "Bitte prüfe das Serverterminal und versuche es erneut."
            );
            return;
        }

        const renderedOutcome = resultOrError.matches(
            "[data-analysis-error]"
        )
            ? "error"
            : "success";

        analysisResponse.replaceChildren(responseTemplate.content);
        analysisResponse.dataset.analysisOutcome =
            responseOutcome || renderedOutcome;
        analysisResponse.hidden = false;
        localizeResultTimes(analysisResponse);

        resultOrError.tabIndex = -1;
        resultOrError.focus({preventScroll: true});
        resultOrError.scrollIntoView({
            behavior: "smooth",
            block: "start",
        });
    }

    function setCriterionRefreshControlsDisabled(disabled) {
        analysisResponse
            .querySelectorAll("button")
            .forEach((button) => {
                button.disabled = disabled;
            });

        if (submitButton) {
            submitButton.disabled = disabled;
        }
    }

    function setCriterionRefreshStatus(
        card,
        message,
        tone = "neutral"
    ) {
        const status = card?.querySelector(
            "[data-criterion-refresh-status]"
        );

        if (!status) {
            return;
        }

        status.textContent = message;
        status.hidden = false;
        status.classList.remove(
            "is-neutral",
            "is-success",
            "is-error"
        );
        status.classList.add(`is-${tone}`);
    }

    async function refreshCriterion(button) {
        if (
            criterionRefreshInFlight ||
            form.dataset.submitting === "true"
        ) {
            return;
        }

        const card = button.closest(
            "[data-criterion-feedback-card]"
        );
        const refreshUrl = button.dataset.refreshUrl;

        if (!card || !refreshUrl) {
            return;
        }

        criterionRefreshInFlight = true;
        form.dataset.criterionRefreshing = "true";
        setCriterionRefreshControlsDisabled(true);
        button.textContent = "Wird aktualisiert …";
        const progress = card.querySelector(
            "[data-criterion-refresh-progress]"
        );

        if (progress) {
            progress.hidden = false;
        }
        setCriterionRefreshStatus(
            card,
            "Dieses Kriterium wird mit einem eigenen Modellaufruf neu analysiert. Bitte lass das Browserfenster geöffnet."
        );

        try {
            const response = await fetch(refreshUrl, {
                method: "POST",
                body: new FormData(form),
                headers: {
                    Accept: "text/html",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });

            if (
                response.redirected &&
                new URL(response.url).pathname === "/login"
            ) {
                window.location.assign("/login");
                return;
            }

            const responseHtml = await response.text();
            const responseTemplate = document.createElement("template");
            responseTemplate.innerHTML = responseHtml.trim();
            const errorElement = responseTemplate.content.querySelector(
                "[data-criterion-refresh-error]"
            );
            const outcome = response.headers.get(
                "X-Criterion-Refresh-Outcome"
            );

            if (errorElement || outcome === "error") {
                setCriterionRefreshStatus(
                    card,
                    errorElement?.textContent.trim() ||
                        "Das Kriterium konnte nicht aktualisiert werden.",
                    "error"
                );
                return;
            }

            const replacement = responseTemplate.content.querySelector(
                "[data-criterion-feedback-card]"
            );

            if (!replacement) {
                throw new Error(
                    "Der Server hat keinen aktualisierten Kriterienbefund zurückgegeben."
                );
            }

            const overallTemplate = responseTemplate.content.querySelector(
                "[data-criterion-refresh-overall]"
            );
            const refreshedOverall = overallTemplate?.content.querySelector(
                "p"
            );
            const currentOverall = analysisResponse.querySelector(
                "[data-overall-feedback-text]"
            );

            if (refreshedOverall && currentOverall) {
                currentOverall.innerHTML = refreshedOverall.innerHTML;
            }

            const refreshCountElement = responseTemplate.content.querySelector(
                "[data-criterion-refresh-count]"
            );
            const refreshSummary = analysisResponse.querySelector(
                "[data-criterion-refresh-summary]"
            );
            const refreshCount = Number(
                refreshCountElement?.dataset.criterionRefreshCount
            );

            if (
                refreshSummary &&
                Number.isInteger(refreshCount) &&
                refreshCount > 0
            ) {
                refreshSummary.textContent =
                    refreshCount === 1
                        ? "Bisher wurde eine Kriterienkarte einzeln aktualisiert."
                        : `Bisher wurden ${refreshCount} Kriterienkarten einzeln aktualisiert.`;
                refreshSummary.hidden = false;
            }

            card.replaceWith(replacement);
            replacement.tabIndex = -1;
            replacement.focus({preventScroll: true});
            replacement.scrollIntoView({
                behavior: "smooth",
                block: "center",
            });
        } catch (error) {
            console.error(
                "Einzelnes Kriterienfeedback konnte nicht aktualisiert werden.",
                error
            );
            setCriterionRefreshStatus(
                card,
                error instanceof Error
                    ? error.message
                    : "Die Einzelaktualisierung konnte nicht abgeschlossen werden.",
                "error"
            );
        } finally {
            criterionRefreshInFlight = false;
            delete form.dataset.criterionRefreshing;
            setCriterionRefreshControlsDisabled(false);

            if (button.isConnected) {
                button.textContent = "Aktualisieren";
            }

            if (progress?.isConnected) {
                progress.hidden = true;
            }

            if (providerSelect.value === "runpod") {
                void loadRunpodStatus();
                void loadRunpodJobs();
            }
        }
    }

    function localizeResultTimes(root = document) {
        root
            .querySelectorAll("[data-local-datetime]")
            .forEach((element) => {
                const value = element.dataset.localDatetime;

                if (value) {
                    element.textContent =
                        `${formatLocalTime(value)} Uhr`;
                }
            });
    }

    function populateResultTechnical(snapshot) {
        const details = document.querySelector(
            "[data-runpod-result-details]"
        );

        if (
            !details ||
            details.dataset.endpointKey !==
                snapshot?.endpoint?.key
        ) {
            return;
        }

        const supplyField = details.querySelector(
            "[data-result-supply]"
        );
        const actualGpuField = details.querySelector(
            "[data-result-actual-gpu]"
        );
        const releaseField = details.querySelector(
            "[data-result-release]"
        );
        const dataCenterField = details.querySelector(
            "[data-result-datacenter]"
        );
        const activeWorkersField = details.querySelector(
            "[data-result-active-workers]"
        );
        const idleTimeoutField = details.querySelector(
            "[data-result-idle-timeout]"
        );
        const workerLimitField = details.querySelector(
            "[data-result-worker-limit]"
        );
        const flashbootField = details.querySelector(
            "[data-result-flashboot]"
        );

        if (supplyField) {
            supplyField.textContent =
                snapshot.supply?.label || "Nicht abrufbar";
        }

        const configuration = snapshot.configuration;

        if (configuration?.available) {
            idleTimeoutField.textContent =
                Number.isInteger(configuration.idleTimeoutSeconds)
                    ? `${Math.round(
                        configuration.idleTimeoutSeconds / 60
                    )} Minuten (${configuration.idleTimeoutSeconds} s)`
                    : "Nicht gemeldet";
            workerLimitField.textContent =
                configuration.minimumWorkers !== null &&
                configuration.maximumWorkers !== null
                    ? `${configuration.minimumWorkers} bis ${configuration.maximumWorkers}`
                    : "Nicht gemeldet";
            flashbootField.textContent =
                configuration.flashboot || "Nicht gemeldet";
        } else {
            const configurationMessage =
                configuration?.message || "Nicht abrufbar";
            idleTimeoutField.textContent = configurationMessage;
            workerLimitField.textContent = "Nicht abrufbar";
            flashbootField.textContent = "Nicht abrufbar";
        }

        const technical = snapshot.technical;

        if (!technical?.available) {
            const unavailableMessage =
                technical?.message || "Nicht abrufbar";

            actualGpuField.textContent = technical?.aggregateAvailable
                ? "Nicht einzeln abrufbar"
                : unavailableMessage;
            releaseField.textContent = "Nicht abrufbar";
            dataCenterField.textContent = "Nicht abrufbar";
            activeWorkersField.replaceChildren();
            addTextElement(
                activeWorkersField,
                "p",
                unavailableMessage,
                "technical-unavailable"
            );

            if (technical?.aggregateAvailable) {
                addTextElement(
                    activeWorkersField,
                    "p",
                    `Aggregierter Status: ${formatWorkerCounts(
                        technical.counts
                    )}`,
                    "technical-summary"
                );
            }
            return;
        }

        const workers = Array.isArray(technical.workers)
            ? technical.workers
            : [];
        const reportedWorkerId = details.dataset.workerId;
        const matchedWorker = reportedWorkerId
            ? workers.find(
                (worker) => worker.id === reportedWorkerId
            )
            : null;

        if (matchedWorker) {
            actualGpuField.textContent =
                matchedWorker.gpuTypeId || "Nicht gemeldet";
            releaseField.textContent =
                matchedWorker.version === null
                    ? "Nicht gemeldet"
                    : `Version ${matchedWorker.version}`;
            dataCenterField.textContent =
                matchedWorker.dataCenterId || "Nicht gemeldet";
        } else {
            actualGpuField.textContent =
                "Nicht eindeutig zuordenbar";
            releaseField.textContent =
                technical.endpointVersion === null
                    ? "Nicht eindeutig zuordenbar"
                    : `Endpoint-Version ${technical.endpointVersion}; Job nicht zugeordnet`;
            dataCenterField.textContent =
                "Nicht eindeutig zuordenbar";
        }

        activeWorkersField.replaceChildren();

        if (!workers.length) {
            addTextElement(
                activeWorkersField,
                "p",
                hasAggregateWorkers(technical.counts)
                    ? "Aktive Worker sind nur aggregiert gemeldet; eine Einzelzuordnung ist nicht verfügbar."
                    : "Nach Abschluss ist kein aktiver Worker mehr gemeldet.",
                "technical-unavailable"
            );
            return;
        }

        addTextElement(
            activeWorkersField,
            "h4",
            "Aktive Worker nach Abschluss"
        );
        const list = document.createElement("ul");
        list.className = "worker-list compact-worker-list";

        workers.forEach((worker) => {
            const item = document.createElement("li");
            item.textContent = [
                worker.gpuTypeId || "GPU nicht gemeldet",
                worker.status || "Status unbekannt",
                worker.id ? `Worker ${worker.id}` : null,
                worker.version === null
                    ? null
                    : `Release ${worker.version}`,
                worker.dataCenterId || null,
            ]
                .filter(Boolean)
                .join(" · ");
            list.append(item);
        });

        activeWorkersField.append(list);
    }

    async function submitAnalysis(event) {
        if (
            typeof window.fetch !== "function" ||
            typeof window.FormData !== "function"
        ) {
            return;
        }

        event.preventDefault();

        if (form.dataset.submitting === "true") {
            return;
        }
        if (form.dataset.criterionRefreshing === "true") {
            return;
        }

        const runpodSelected =
            providerSelect.value === "runpod";
        const formData = new FormData(form);

        beginLoading();

        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: formData,
                headers: {
                    Accept: "text/html",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });

            if (
                response.redirected &&
                new URL(response.url).pathname === "/login"
            ) {
                window.location.assign("/login");
                return;
            }

            const responseHtml = await response.text();
            const responseOutcome = response.headers.get(
                "X-Analysis-Outcome"
            );

            if (!response.ok && !responseHtml.trim()) {
                throw new Error(
                    "Die Anfrage konnte nicht abgeschlossen werden. Bitte lade die Seite neu und versuche es erneut."
                );
            }

            renderAnalysisResponse(responseHtml, responseOutcome);
        } catch (error) {
            console.error("Feedbackanfrage fehlgeschlagen.", error);
            const message =
                error instanceof Error
                    ? error.message
                    : "Die Anfrage konnte nicht abgeschlossen werden.";
            renderClientError(message);
        } finally {
            resetLoadingState();

            if (runpodSelected) {
                void loadRunpodStatus();
            }
        }
    }

    if (taskSelect) {
        taskSelect.addEventListener("change", updateTaskPreview);
    }

    if (advancedOptionsToggle) {
        advancedOptionsToggle.addEventListener("change", () => {
            updateAdvancedOptionsVisibility();
            updateTaskPreview();
            updateProviderPanels();
        });
    }

    providerSelect.addEventListener("change", updateProviderPanels);

    modelSelects.forEach((modelSelect) => {
        modelSelect.addEventListener("change", () => {
            updateCustomModelField(modelSelect);
        });
    });

    if (loadOllamaModelsButton) {
        loadOllamaModelsButton.addEventListener(
            "click",
            loadOllamaModels
        );
    }

    if (runpodEndpointSelect) {
        runpodEndpointSelect.addEventListener("change", () => {
            latestRunpodSnapshot = null;
            currentRunpodJobId = null;
            currentRunpodJobStatus = null;
            void loadRunpodStatus();
            void loadRunpodJobs();
        });
    }

    if (refreshRunpodStatusButton) {
        refreshRunpodStatusButton.addEventListener(
            "click",
            () => {
                void loadRunpodStatus();
                void loadRunpodJobs();
            }
        );
    }

    if (runpodJobManager) {
        runpodJobManager.addEventListener("toggle", () => {
            if (runpodJobManager.open) {
                void loadRunpodJobs();
            }
        });
    }

    if (cancelManualRunpodJobButton && manualRunpodJobId) {
        cancelManualRunpodJobButton.addEventListener(
            "click",
            () => {
                const jobId = manualRunpodJobId.value.trim();

                if (!manualRunpodJobId.reportValidity() || !jobId) {
                    setRunpodJobManagerStatus(
                        "Bitte gib eine gültige RunPod-Request-ID ein.",
                        "error"
                    );
                    return;
                }

                void cancelRunpodJob(
                    jobId,
                    cancelManualRunpodJobButton
                );
            }
        );
    }

    if (cancelCurrentRunpodJobButton) {
        cancelCurrentRunpodJobButton.addEventListener(
            "click",
            () => {
                if (currentRunpodJobId) {
                    void cancelRunpodJob(
                        currentRunpodJobId,
                        cancelCurrentRunpodJobButton
                    );
                }
            }
        );
    }

    analysisResponse.addEventListener("click", (event) => {
        const button = event.target.closest(
            "[data-refresh-criterion]"
        );

        if (button && analysisResponse.contains(button)) {
            void refreshCriterion(button);
        }
    });

    form.addEventListener("submit", submitAnalysis);

    window.addEventListener("pageshow", () => {
        resetLoadingState();
        localizeResultTimes();

        if (
            providerSelect.value === "runpod" &&
            latestRunpodSnapshot === null
        ) {
            void loadRunpodStatus();

            if (advancedOptionsEnabled()) {
                void loadRunpodJobs();
            }
        }
    });

    updateTaskPreview();
    updateProviderPanels();
    localizeResultTimes();
})();
