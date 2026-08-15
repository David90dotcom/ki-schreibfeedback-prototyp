(() => {
    "use strict";

    const MODEL_CHECK_DELAY_MS = 3000;
    const SLOW_EVALUATION_DELAY_MS = 30000;
    const VERY_SLOW_EVALUATION_DELAY_MS = 90000;
    const forms = document.querySelectorAll(
        "[data-automatic-evaluation-form]"
    );
    const deleteForms = document.querySelectorAll(
        "[data-confirm-evaluation-delete]"
    );
    const removeRunForms = document.querySelectorAll(
        "[data-confirm-feedback-run-remove]"
    );
    const formStates = new Map();

    function formatElapsed(milliseconds) {
        const totalSeconds = Math.max(
            0,
            Math.floor(milliseconds / 1000)
        );
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;

        if (minutes === 0) {
            return `${seconds} s`;
        }

        return `${minutes} min ${String(seconds).padStart(2, "0")} s`;
    }

    function updateElapsed(form) {
        const state = formStates.get(form);

        if (!state) {
            return;
        }

        const elapsed = formatElapsed(
            Date.now() - state.startedAt
        );
        state.message.textContent = `${state.liveMessage} – ${elapsed}`;
    }

    function setLiveMessage(form, message, hint) {
        const state = formStates.get(form);

        if (!state) {
            return;
        }

        state.liveMessage = message;
        state.hint.textContent = hint;
        updateElapsed(form);
    }

    function clearFormTimers(state) {
        window.clearInterval(state.elapsedTimer);
        state.phaseTimers.forEach((timer) => {
            window.clearTimeout(timer);
        });
    }

    function resetForm(form) {
        const state = formStates.get(form);
        const button = form.querySelector(
            "[data-automatic-evaluation-button]"
        );
        const loading = form.querySelector(
            "[data-automatic-evaluation-loading]"
        );

        if (state) {
            clearFormTimers(state);
            formStates.delete(form);
        }

        delete form.dataset.submitting;
        form.removeAttribute("aria-busy");

        if (button) {
            button.disabled = false;
            button.textContent =
                button.dataset.defaultLabel ||
                "Jetzt automatisch vorbewerten";
        }

        if (loading) {
            loading.hidden = true;
        }
    }

    function showClientError(form, message) {
        const error = form.querySelector(
            "[data-automatic-evaluation-client-error]"
        );
        const errorMessage = form.querySelector(
            "[data-automatic-evaluation-client-error-message]"
        );

        if (!error || !errorMessage) {
            return;
        }

        errorMessage.textContent = message;
        error.hidden = false;
        error.scrollIntoView({
            behavior: "smooth",
            block: "nearest",
        });
    }

    async function submitWithVisibleProgress(event) {
        const form = event.currentTarget;

        if (!(form instanceof HTMLFormElement)) {
            return;
        }

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

        if (!form.checkValidity()) {
            form.reportValidity();
            return;
        }

        const button = form.querySelector(
            "[data-automatic-evaluation-button]"
        );
        const loading = form.querySelector(
            "[data-automatic-evaluation-loading]"
        );
        const message = form.querySelector(
            "[data-automatic-evaluation-message]"
        );
        const hint = form.querySelector(
            "[data-automatic-evaluation-hint]"
        );

        if (!button || !loading || !message || !hint) {
            HTMLFormElement.prototype.submit.call(form);
            return;
        }

        const clientError = form.querySelector(
            "[data-automatic-evaluation-client-error]"
        );

        if (clientError) {
            clientError.hidden = true;
        }

        const modelSelect = form.querySelector(
            "[data-automatic-evaluation-model]"
        );
        const model =
            modelSelect instanceof HTMLSelectElement
                ? modelSelect.value
                : form.dataset.evaluationModel ||
                  "Das Bewertungsmodell";
        const state = {
            button,
            loading,
            message,
            hint,
            startedAt: Date.now(),
            liveMessage: "Bewertungsdaten werden an OpenAI übertragen …",
            elapsedTimer: 0,
            phaseTimers: [],
        };

        formStates.set(form, state);
        form.dataset.submitting = "true";
        form.setAttribute("aria-busy", "true");
        button.disabled = true;
        button.textContent = "Vorbewertung läuft …";
        loading.hidden = false;
        hint.textContent =
            "Bitte lass dieses Browserfenster geöffnet. Das Ergebnis wird nach Abschluss automatisch angezeigt.";
        updateElapsed(form);

        state.elapsedTimer = window.setInterval(
            () => updateElapsed(form),
            1000
        );
        state.phaseTimers.push(
            window.setTimeout(() => {
                setLiveMessage(
                    form,
                    `${model} untersucht das Feedback detailliert …`,
                    "Das Modell gleicht Aussagen, Belege und ausgelassene Probleme mit der vollständigen Bewertungsgrundlage ab."
                );
            }, MODEL_CHECK_DELAY_MS),
            window.setTimeout(() => {
                setLiveMessage(
                    form,
                    "Die gründliche Evidenzprüfung läuft weiterhin …",
                    "Der gewählte Denkaufwand kann länger dauern als die normale Feedback-Erzeugung. Die Anfrage ist weiterhin aktiv."
                );
            }, SLOW_EVALUATION_DELAY_MS),
            window.setTimeout(() => {
                setLiveMessage(
                    form,
                    "Das Bewertungsmodell arbeitet weiterhin …",
                    "Bitte warte weiter und klicke nicht erneut. Nach Abschluss wird die manuelle Prüfmaske automatisch geöffnet und vorausgefüllt."
                );
            }, VERY_SLOW_EVALUATION_DELAY_MS)
        );

        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                headers: {
                    Accept: "text/html",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });

            const responseUrl = new URL(
                response.url,
                window.location.href
            );

            if (
                response.redirected &&
                responseUrl.pathname === "/login"
            ) {
                window.location.assign(responseUrl.toString());
                return;
            }

            if (
                !response.ok ||
                responseUrl.pathname !== "/feedback-evaluations"
            ) {
                throw new Error(
                    "Die Anfrage konnte nicht abgeschlossen werden. Bitte prüfe das Serverterminal und versuche es erneut."
                );
            }

            const feedbackRunId = form.dataset.feedbackRunId;

            if (feedbackRunId) {
                responseUrl.hash = `feedback-run-${feedbackRunId}`;
            }

            window.location.assign(responseUrl.toString());
        } catch (error) {
            const errorMessage =
                error instanceof Error
                    ? error.message
                    : "Die automatische Vorbewertung konnte nicht gestartet werden.";
            resetForm(form);
            showClientError(form, errorMessage);
        }
    }

    function revealPrefilledManualEvaluation() {
        const details = document.querySelector(
            "[data-open-after-automatic-evaluation]"
        );

        if (!(details instanceof HTMLDetailsElement)) {
            return;
        }

        details.open = true;
        const reduceMotion = window.matchMedia(
            "(prefers-reduced-motion: reduce)"
        ).matches;

        window.requestAnimationFrame(() => {
            details.scrollIntoView({
                behavior: reduceMotion ? "auto" : "smooth",
                block: "start",
            });
        });
    }

    forms.forEach((form) => {
        form.addEventListener(
            "submit",
            submitWithVisibleProgress
        );
    });

    deleteForms.forEach((form) => {
        form.addEventListener("submit", (event) => {
            const label =
                form.dataset.evaluationLabel || "diese Bewertung";
            const confirmed = window.confirm(
                `Möchtest du „${label}“ wirklich löschen? ` +
                "Dieser Bewertungsdatensatz kann anschließend nicht wiederhergestellt werden."
            );

            if (!confirmed) {
                event.preventDefault();
            }
        });
    });

    removeRunForms.forEach((form) => {
        form.addEventListener("submit", (event) => {
            const label =
                form.dataset.feedbackRunLabel || "diesen Feedbackbogen";
            const evaluationCount = Number.parseInt(
                form.dataset.evaluationCount || "0",
                10
            );
            const evaluationWarning =
                evaluationCount > 0
                    ? ` Dabei werden auch ${evaluationCount} gespeicherte ` +
                      `${evaluationCount === 1 ? "Bewertung" : "Bewertungen"} gelöscht.`
                    : "";
            const confirmed = window.confirm(
                `Möchtest du den Feedbackbogen „${label}“ wirklich aus ` +
                `der Feedback-Bewertung entfernen?${evaluationWarning} ` +
                "Der gespeicherte Schülertext wird entfernt. Dieser Schritt kann nicht rückgängig gemacht werden."
            );

            if (!confirmed) {
                event.preventDefault();
            }
        });
    });

    window.addEventListener("pageshow", () => {
        forms.forEach(resetForm);
    });

    revealPrefilledManualEvaluation();
})();
