(() => {
    const form = document.getElementById("authForm");
    const loginTab = document.getElementById("loginTab");
    const registerTab = document.getElementById("registerTab");
    const title = document.getElementById("authTitle");
    const subtitle = document.getElementById("authSubtitle");
    const button = document.getElementById("authButton");
    const buttonLabel = button.querySelector("span");
    const errorBox = document.getElementById("authError");
    const password = document.getElementById("password");
    const reveal = document.getElementById("revealPassword");
    let mode = "login";

    function setMode(nextMode) {
        mode = nextMode;
        const registering = mode === "register";
        loginTab.classList.toggle("active", !registering);
        registerTab.classList.toggle("active", registering);
        title.textContent = registering ? "创建你的个人空间" : "登录你的空间";
        subtitle.textContent = registering
            ? "从一份简短问卷开始，建立只属于你的日常节律。"
            : "继续查看今天的节律与支持建议。";
        buttonLabel.textContent = registering ? "创建账号" : "登录";
        password.autocomplete = registering ? "new-password" : "current-password";
        errorBox.hidden = true;
    }

    loginTab.addEventListener("click", () => setMode("login"));
    registerTab.addEventListener("click", () => setMode("register"));
    reveal.addEventListener("click", () => {
        const visible = password.type === "text";
        password.type = visible ? "password" : "text";
        reveal.textContent = visible ? "显示" : "隐藏";
        reveal.setAttribute("aria-label", visible ? "显示密码" : "隐藏密码");
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorBox.hidden = true;
        if (!form.reportValidity()) return;
        button.disabled = true;
        buttonLabel.textContent = mode === "register" ? "正在创建…" : "正在登录…";
        try {
            const response = await fetch(`/api/auth/${mode}`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    login_id: document.getElementById("loginId").value.trim(),
                    password: password.value
                })
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.message || "操作未完成，请稍后再试");
            window.location.assign("/");
        } catch (error) {
            errorBox.textContent = error.message;
            errorBox.hidden = false;
        } finally {
            button.disabled = false;
            buttonLabel.textContent = mode === "register" ? "创建账号" : "登录";
        }
    });
})();
