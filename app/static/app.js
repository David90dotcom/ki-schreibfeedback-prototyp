(() => {
    "use strict";

    const CUSTOM_MODEL_VALUE = "__custom__";
    const SLOW_RESPONSE_DELAY_MS = 8000;

    const form = document.querySelector("#analysis-form");

    if (!form) {
        return;
    }

    const providerSelect =
        form.querySelector("#provider");

    const providerPanels =
        form.querySelectorAll("[data-provider-panel]");

    const modelSelects =
        form.querySelectorAll("[data-model-select]");

    const ollamaBaseUrlInput =
        form.querySelector("#ollama-base-url");

    const ollamaModelSelect =
        form.querySelector("#ollama-model");

    const loadOllamaModelsButton =
        form.querySelector("#load-ollama-models");

    const ollamaStatus =
        form.querySelector("#ollama-model-status");

    const submitButton =
        form.querySelector("#submit-button");

    const loadingIndicator =
        form.querySelector("#analysis-loading");

    const loadingMessage =
        form.querySelector("#loading-message");

    const loadingHint =
        form.querySelector("#loading-hint");

    let slowResponseTimer = null;

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

        customGroup.hidden = !customSelected;

        customInput.disabled =
            !customSelected || !panelActive;

        customInput.required =
            customSelected && panelActive;
    }

    function updateProviderPanels() {
        const selectedProvider =
            providerSelect.value;

        providerPanels.forEach((panel) => {
            const panelActive =
                panel.dataset.providerPanel ===
                selectedProvider;

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

        if (ollamaBaseUrlInput) {
            ollamaBaseUrlInput.required =
                selectedProvider === "ollama";
        }
    }

    function setOllamaStatus(message, status) {
        if (!ollamaStatus) {
            return;
        }

        ollamaStatus.textContent = message;
        ollamaStatus.className = "status-message";

        if (status) {
            ollamaStatus.classList.add(
                `status-${status}`
            );
        }
    }

    function addModelOption(modelName, label) {
        const option =
            document.createElement("option");

        option.value = modelName;
        option.textContent = label;

        ollamaModelSelect.append(option);
    }

    function replaceOllamaModelOptions(
        modelNames,
        defaultModel
    ) {
        const currentSelection =
            ollamaModelSelect.value;

        const discoveredModels = [
            ...new Set(modelNames),
        ]
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

        addModelOption(
            defaultModel,
            defaultLabel
        );

        discoveredModels.forEach((modelName) => {
            if (modelName !== defaultModel) {
                addModelOption(
                    modelName,
                    modelName
                );
            }
        });

        addModelOption(
            CUSTOM_MODEL_VALUE,
            "Andere Modell-ID …"
        );

        if (
            currentSelection === CUSTOM_MODEL_VALUE
        ) {
            ollamaModelSelect.value =
                CUSTOM_MODEL_VALUE;
        } else if (
            currentSelection === defaultModel ||
            discoveredModels.includes(
                currentSelection
            )
        ) {
            ollamaModelSelect.value =
                currentSelection;
        } else {
            ollamaModelSelect.value =
                defaultModel;
        }

        updateCustomModelField(
            ollamaModelSelect
        );
    }

    async function loadOllamaModels() {
        const baseUrl =
            ollamaBaseUrlInput.value.trim();

        setOllamaStatus(
            "Verbindung zu Ollama wird geprüft …",
            null
        );

        loadOllamaModelsButton.disabled = true;

        try {
            const parameters =
                new URLSearchParams({
                    base_url: baseUrl,
                });

            const response = await fetch(
                `/api/ollama/models?${parameters}`,
                {
                    headers: {
                        Accept: "application/json",
                    },
                }
            );

            const payload =
                await response.json();

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

            ollamaBaseUrlInput.value =
                payload.base_url;

            replaceOllamaModelOptions(
                payload.models,
                payload.default_model
            );

            const message =
                payload.models.length
                    ? payload.message
                    : "Ollama ist erreichbar, meldet aber keine installierten Modelle.";

            setOllamaStatus(
                message,
                payload.models.length
                    ? "success"
                    : "warning"
            );
        } catch (error) {
            const message =
                error instanceof Error
                    ? error.message
                    : "Die Ollama-Modellliste konnte nicht geladen werden.";

            setOllamaStatus(
                message,
                "error"
            );
        } finally {
            loadOllamaModelsButton.disabled =
                providerSelect.value !== "ollama";
        }
    }

    function startLoading(event) {
        if (form.dataset.submitting === "true") {
            event.preventDefault();
            return;
        }

        form.dataset.submitting = "true";
        form.setAttribute("aria-busy", "true");

        submitButton.disabled = true;
        submitButton.textContent =
            "Feedback wird generiert …";

        loadingIndicator.hidden = false;

        const localModelSelected =
            providerSelect.value === "ollama";

        loadingMessage.textContent =
            localModelSelected
                ? "Das lokale Modell verarbeitet den Text …"
                : "Das Cloudmodell verarbeitet den Text …";

        loadingHint.textContent =
            localModelSelected
                ? "Bitte warten. Der erste Aufruf kann länger dauern, weil das Modell zunächst in den Arbeitsspeicher geladen wird."
                : "Bitte warten. Die Dauer hängt vom ausgewählten Modell und der Verbindung ab.";

        slowResponseTimer = window.setTimeout(
            () => {
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
            },
            SLOW_RESPONSE_DELAY_MS
        );
    }

    function resetLoadingState() {
        if (slowResponseTimer !== null) {
            window.clearTimeout(
                slowResponseTimer
            );

            slowResponseTimer = null;
        }

        delete form.dataset.submitting;
        form.removeAttribute("aria-busy");

        submitButton.disabled = false;

        submitButton.textContent =
            submitButton.dataset.defaultLabel ||
            "Feedback generieren";

        loadingIndicator.hidden = true;
    }

    providerSelect.addEventListener(
        "change",
        updateProviderPanels
    );

    modelSelects.forEach((modelSelect) => {
        modelSelect.addEventListener(
            "change",
            () => {
                updateCustomModelField(
                    modelSelect
                );
            }
        );
    });

    loadOllamaModelsButton.addEventListener(
        "click",
        loadOllamaModels
    );

    form.addEventListener(
        "submit",
        startLoading
    );

    window.addEventListener(
        "pageshow",
        resetLoadingState
    );

    updateProviderPanels();
})();