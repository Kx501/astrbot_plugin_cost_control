/** One writer drains the latest committed draft, even if it changed during a request. */
export class AutoSaveQueue<T> {
  private current: { payload: T; key: string } | null = null;
  private saved: string | null = null;
  private enabled = false;
  private running: Promise<void> | null = null;

  constructor(private readonly save: (payload: T) => Promise<void>) {}

  update(payload: T, enabled: boolean): void {
    const key = JSON.stringify(payload);
    if (enabled && !this.enabled) this.saved = key;
    this.enabled = enabled;
    this.current = { payload, key };
  }

  isDirty(): boolean {
    return this.running !== null || this.needsSave();
  }

  private needsSave(): boolean {
    return this.enabled && this.current !== null && this.current.key !== this.saved;
  }

  flush(): Promise<void> {
    if (this.running) return this.running;
    if (!this.needsSave()) return Promise.resolve();
    let resolve!: () => void;
    let reject!: (reason: unknown) => void;
    const pending = new Promise<void>((done, fail) => {
      resolve = done;
      reject = fail;
    });
    // Register the writer before starting it, but start synchronously: on unmount
    // the new page's configuration read must see this pending write immediately.
    this.running = pending.finally(() => {
      this.running = null;
    });
    const drain = async () => {
      while (this.needsSave()) {
        const snapshot = this.current!;
        await this.save(snapshot.payload);
        this.saved = snapshot.key;
      }
    };
    void drain().then(resolve, reject);
    return this.running;
  }
}
