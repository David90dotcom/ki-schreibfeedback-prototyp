(() => {
    "use strict";

    document.querySelectorAll("[data-confirm-delete]").forEach(
        (form) => {
            form.addEventListener("submit", (event) => {
                const confirmed = window.confirm(
                    "Möchtest du diese Feedback-Vorlage wirklich löschen? " +
                    "Bereits erstellte Analyseergebnisse bleiben erhalten."
                );

                if (!confirmed) {
                    event.preventDefault();
                }
            });
        }
    );

    const form = document.querySelector("#task-form");

    if (!form) {
        return;
    }

    const criteriaList = form.querySelector("[data-criteria-list]");
    const addButton = form.querySelector("[data-add-criterion]");

    if (!criteriaList || !addButton) {
        return;
    }

    const maxCriteria = Number.parseInt(
        criteriaList.dataset.maxCriteria || "100",
        10
    );
    const maxCriterionChars = Number.parseInt(
        criteriaList.dataset.maxCriterionChars || "10000",
        10
    );

    function items() {
        return [...criteriaList.querySelectorAll("[data-criterion-item]")];
    }

    function updateControls() {
        const currentItems = items();

        currentItems.forEach((item, index) => {
            const number = item.querySelector("[data-criterion-number]");
            const moveUp = item.querySelector("[data-move-up]");
            const moveDown = item.querySelector("[data-move-down]");
            const remove = item.querySelector("[data-remove-criterion]");

            if (number) {
                number.textContent = String(index + 1);
            }
            if (moveUp) {
                moveUp.disabled = index === 0;
            }
            if (moveDown) {
                moveDown.disabled = index === currentItems.length - 1;
            }
            if (remove) {
                remove.disabled = currentItems.length === 1;
            }
        });

        addButton.disabled = currentItems.length >= maxCriteria;
    }

    function createCriterionItem() {
        const wrapper = document.createElement("div");
        wrapper.className = "criterion-editor-item";
        wrapper.dataset.criterionItem = "";

        const labelRow = document.createElement("div");
        labelRow.className = "criterion-editor-label";

        const label = document.createElement("label");
        label.append("Kriterium ");
        const number = document.createElement("span");
        number.dataset.criterionNumber = "";
        label.append(number);

        const controls = document.createElement("div");
        controls.className = "criterion-order-controls";

        const buttonDefinitions = [
            ["Nach oben", "moveUp"],
            ["Nach unten", "moveDown"],
            ["Entfernen", "removeCriterion"],
        ];

        buttonDefinitions.forEach(([text, dataName]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = text;
            button.className =
                dataName === "removeCriterion"
                    ? "danger-button compact-button"
                    : "secondary-button compact-button";
            button.dataset[dataName] = "";
            controls.append(button);
        });

        labelRow.append(label, controls);

        const titleLabel = document.createElement("label");
        titleLabel.className = "criterion-editor-field";
        const titleCaption = document.createElement("span");
        titleCaption.textContent = "Überschrift in der Textanalyse";
        const titleInput = document.createElement("input");
        titleInput.name = "criterion_titles";
        titleInput.type = "text";
        titleInput.maxLength = 120;
        titleInput.required = true;
        titleInput.placeholder = "zum Beispiel: Einleitung: Thema";
        titleLabel.append(titleCaption, titleInput);

        const criterionLabel = document.createElement("label");
        criterionLabel.className = "criterion-editor-field";
        const criterionCaption = document.createElement("span");
        criterionCaption.textContent = "Prüfkriterium";
        const textarea = document.createElement("textarea");
        textarea.name = "criteria";
        textarea.rows = 3;
        textarea.maxLength = maxCriterionChars;
        textarea.required = true;
        textarea.placeholder =
            "zum Beispiel: Du hast einen Einleitungssatz mit Titel, " +
            "Autor, Textart und Thema verfasst.";
        criterionLabel.append(criterionCaption, textarea);

        wrapper.append(labelRow, titleLabel, criterionLabel);
        return wrapper;
    }

    addButton.addEventListener("click", () => {
        if (items().length >= maxCriteria) {
            return;
        }

        const item = createCriterionItem();
        criteriaList.append(item);
        updateControls();
        item.querySelector('input[name="criterion_titles"]')?.focus();
    });

    criteriaList.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        const item = button?.closest("[data-criterion-item]");

        if (!button || !item) {
            return;
        }

        if (button.matches("[data-move-up]")) {
            const previous = item.previousElementSibling;

            if (previous) {
                criteriaList.insertBefore(item, previous);
            }
        } else if (button.matches("[data-move-down]")) {
            const next = item.nextElementSibling;

            if (next) {
                criteriaList.insertBefore(next, item);
            }
        } else if (
            button.matches("[data-remove-criterion]") &&
            items().length > 1
        ) {
            item.remove();
        }

        updateControls();
    });

    updateControls();
})();
