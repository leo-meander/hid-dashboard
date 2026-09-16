import React from "react";

/**
 * Last line of defence: a render crash shows a message instead of a blank page.
 *
 * The crash worth naming is not ours. Anything that rewrites the page's text
 * nodes from outside React — Chrome's translate-this-page, and a few
 * extensions that do the same — leaves React holding references to nodes that
 * have been moved inside a wrapper element. The next subtree React removes
 * then throws
 *
 *     NotFoundError: Failed to execute 'removeChild' on 'Node':
 *     The node to be removed is not a child of this node.
 *
 * and, with nothing to catch it, React unmounts the whole tree: the tab goes
 * white and the only way back is a reload the reader has to guess at. On Fill
 * Pace, ticking a second stay month is enough to trigger it, because the month
 * table is a subtree that comes and goes.
 *
 * `translate="no"` in index.html stops the common cause. This stops the blank
 * page for every other cause, and says out loud what is worth trying, because
 * a reader who has translation on cannot tell that from a white screen.
 *
 * Reloading rather than re-rendering in place is deliberate. By the time this
 * fires the DOM no longer matches what React believes about it, and rendering
 * into the same container tends to fail the same way a moment later.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("HiD crashed:", error, info?.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;

    const dom = /removeChild|insertBefore|not a child of this node/i
      .test(String(this.state.error?.message || ""));

    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 p-6">
        <div className="max-w-md w-full bg-white border border-gray-200 rounded-xl p-5">
          <h1 className="font-semibold text-gray-800">This page stopped rendering</h1>
          <p className="text-sm text-gray-600 mt-2">
            {dom
              ? "Something outside the dashboard rewrote the page while it was updating. "
                + "This is almost always the browser's translate-this-page feature, or an "
                + "extension that edits text. Turn translation off for this site and reload."
              : "An unexpected error stopped the page. Reloading usually clears it."}
          </p>
          <button
            onClick={() => window.location.reload()}
            className="mt-4 px-3 py-1.5 text-sm rounded-lg bg-gray-900 text-white"
          >
            Reload
          </button>
          <pre className="mt-3 text-[11px] text-gray-400 whitespace-pre-wrap break-words">
            {String(this.state.error?.message || this.state.error)}
          </pre>
        </div>
      </div>
    );
  }
}
