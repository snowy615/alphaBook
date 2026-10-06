// Shared "verification needed" gate for signup.html and login.html. Both
// pages already have a #auth-container div holding their form — this
// replaces that with a pending panel and resolves once Firebase reports the
// address as verified, so the caller can go get a real session and move on.
export function showVerificationNeeded({ user, email }) {
  const container = document.getElementById("auth-container");
  container.innerHTML = `
    <div class="info" style="display:block;">
      <strong>Verification needed.</strong> We sent a link to ${email}.
      Check your inbox — and your spam/junk folder, it sometimes lands
      there. It can take a few minutes to arrive, especially at an Oxford
      address. This page moves on by itself once you've verified.
      <br><br><strong>Please don't sign up again</strong> with another email or with
      Google unless it really doesn't work: a second account can't see your
      application.
    </div>
    <button type="button" class="btn ghost" id="resendVerifyBtn" style="margin-top:14px;">
      Resend verification email
    </button>`;

  const resendBtn = document.getElementById("resendVerifyBtn");
  const resendDefault = resendBtn.textContent;
  resendBtn.addEventListener("click", async () => {
    resendBtn.disabled = true;
    resendBtn.textContent = "Sending…";
    try {
      const token = await user.getIdToken();
      const formData = new FormData();
      formData.append("id_token", token);
      const res = await fetch("/auth/resend-verification", { method: "POST", body: formData, credentials: "include" });
      resendBtn.textContent = res.ok ? "Sent — check your inbox" : "Couldn't resend, try again";
    } catch (e) {
      resendBtn.textContent = "Couldn't resend, try again";
    }
    setTimeout(() => { resendBtn.disabled = false; resendBtn.textContent = resendDefault; }, 4000);
  });

  return new Promise((resolve) => {
    const interval = setInterval(async () => {
      try {
        await user.reload();
        if (!user.emailVerified) return;
        clearInterval(interval);
        resolve();
      } catch (e) {
        console.error("verification poll failed", e);
      }
    }, 3000);
  });
}
