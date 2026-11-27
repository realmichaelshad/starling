/* Runs before the first paint (not deferred, on purpose): marks the document as scripted so the
   stylesheets can prepare entrance states and the small-screen menu without a flash. */
document.documentElement.classList.add("js");
