export const NotificationService = {
  container: null,
  activeToast: null,
  hideTimeout: null,
  initContainer() {
    if (this.container) return;
    const appWrap = document.getElementById("app-wrap") || document.body;
    this.container = document.createElement("div");
    this.container.id = "notification-container";
    appWrap.appendChild(this.container);
  },
  show(message, duration = 3000) {
    this.initContainer();
    clearTimeout(this.hideTimeout);

    // Reuse the single active toast if one's already showing/animating out,
    // instead of stacking a new one on top of it.
    let toast = this.activeToast;
    if (!toast || !toast.isConnected) {
      toast = document.createElement("div");
      toast.className = "glass-notification";
      toast.style.cursor = "pointer";
      toast.addEventListener("click", () => {
        clearTimeout(this.hideTimeout);
        this.dismiss(toast);
      });
      this.container.appendChild(toast);
      this.activeToast = toast;
      toast.offsetHeight;
    }

    toast.textContent = message;
    toast.classList.add("show");

    this.hideTimeout = setTimeout(() => {
      this.dismiss(toast);
    }, duration);
  },
  dismiss(toast) {
    toast.classList.remove("show");
    toast.addEventListener("transitionend", () => {
      toast.remove();
      if (this.activeToast === toast) this.activeToast = null;
    }, { once: true });
  }
};
