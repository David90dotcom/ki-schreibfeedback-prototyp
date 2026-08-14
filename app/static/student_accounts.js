(() => {
    "use strict";

    const copyButton = document.querySelector("[data-copy-issued-code]");
    const issuedCode = document.querySelector("[data-issued-code]");

    copyButton?.addEventListener("click", async () => {
        const code = issuedCode?.textContent?.trim();

        if (!code) {
            return;
        }

        try {
            await navigator.clipboard.writeText(code);
            copyButton.textContent = "Kopiert";
        } catch (error) {
            console.error("Zugangscode konnte nicht kopiert werden.", error);
            issuedCode.focus();
        }
    });

    document
        .querySelectorAll("[data-confirm-new-code]")
        .forEach((form) => {
            form.addEventListener("submit", (event) => {
                const label = form.dataset.accountLabel || "dieses Konto";
                const confirmed = window.confirm(
                    `Für „${label}“ einen neuen Code erzeugen? ` +
                        "Der bisherige Code wird sofort ungültig."
                );

                if (!confirmed) {
                    event.preventDefault();
                }
            });
        });

    document
        .querySelectorAll("[data-confirm-student-account-delete]")
        .forEach((form) => {
            form.addEventListener("submit", (event) => {
                const label = form.dataset.accountLabel || "dieses Konto";
                const confirmed = window.confirm(
                    `Schülerkonto „${label}“ endgültig löschen?`
                );

                if (!confirmed) {
                    event.preventDefault();
                }
            });
        });
})();
