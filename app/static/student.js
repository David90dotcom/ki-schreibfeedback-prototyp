(() => {
    "use strict";

    const form = document.querySelector("#student-analysis-form");

    if (!form) {
        return;
    }

    const taskSelect = form.querySelector("#student-task-id");
    const submitButton = form.querySelector("#student-submit-button");
    const loadingIndicator = form.querySelector(
        "#student-analysis-loading"
    );
    const responseRegion = document.querySelector(
        "#student-analysis-response"
    );

    function updateTaskPreview() {
        const selectedTaskId = taskSelect?.value || "";

        document
            .querySelectorAll("[data-student-task-preview]")
            .forEach((preview) => {
                preview.hidden =
                    preview.dataset.studentTaskPreview !== selectedTaskId;
            });
    }

    function setLoading(active) {
        form.dataset.submitting = active ? "true" : "false";
        form.setAttribute("aria-busy", active ? "true" : "false");
        responseRegion?.setAttribute(
            "aria-busy",
            active ? "true" : "false"
        );

        if (submitButton) {
            submitButton.disabled = active || !taskSelect?.value;
            submitButton.textContent = active
                ? "Feedback wird erstellt …"
                : "Feedback erstellen";
        }

        if (loadingIndicator) {
            loadingIndicator.hidden = !active;
        }
    }

    function focusResult(element) {
        element.tabIndex = -1;
        element.focus({preventScroll: true});
        element.scrollIntoView({behavior: "smooth", block: "start"});
    }

    function renderClientError(message) {
        if (!responseRegion) {
            return;
        }

        const section = document.createElement("section");
        const heading = document.createElement("h2");
        const paragraph = document.createElement("p");
        section.className = "card error";
        section.dataset.studentAnalysisError = "true";
        heading.textContent = "Feedback nicht verfügbar";
        paragraph.textContent = message;
        section.append(heading, paragraph);
        responseRegion.replaceChildren(section);
        responseRegion.dataset.analysisOutcome = "error";
        focusResult(section);
    }

    function renderResponse(responseHtml, responseOutcome) {
        if (!responseRegion) {
            return;
        }

        const responseTemplate = document.createElement("template");
        responseTemplate.innerHTML = responseHtml.trim();
        const resultOrError = responseTemplate.content.querySelector(
            "[data-student-analysis-result], " +
                "[data-student-analysis-error]"
        );

        if (!resultOrError) {
            renderClientError(
                "Der Server hat keine darstellbare Rückmeldung geliefert. " +
                    "Bitte versuche es erneut."
            );
            return;
        }

        responseRegion.replaceChildren(responseTemplate.content);
        responseRegion.dataset.analysisOutcome =
            responseOutcome ||
            (resultOrError.matches("[data-student-analysis-error]")
                ? "error"
                : "success");
        focusResult(resultOrError);
    }

    async function submitAnalysis(event) {
        event.preventDefault();

        if (form.dataset.submitting === "true" || !form.reportValidity()) {
            return;
        }

        setLoading(true);

        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                headers: {
                    Accept: "text/html",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });

            if (
                response.redirected &&
                new URL(response.url).pathname === "/schueler"
            ) {
                window.location.assign("/schueler");
                return;
            }

            const responseHtml = await response.text();
            renderResponse(
                responseHtml,
                response.headers.get("X-Analysis-Outcome")
            );
        } catch (error) {
            console.error("Schülerfeedbackanfrage fehlgeschlagen.", error);
            renderClientError(
                "Die Verbindung wurde unterbrochen. Bitte prüfe deine " +
                    "Internetverbindung und versuche es erneut."
            );
        } finally {
            setLoading(false);
        }
    }

    taskSelect?.addEventListener("change", () => {
        updateTaskPreview();

        if (submitButton) {
            submitButton.disabled = !taskSelect.value;
        }
    });
    form.addEventListener("submit", submitAnalysis);
    window.addEventListener("pageshow", () => setLoading(false));
    updateTaskPreview();
})();
