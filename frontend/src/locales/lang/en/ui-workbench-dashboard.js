export default {
  title: 'UI automation workbench',
  currentProject: 'Current project',
  selectProject: 'Select a project',
  latestExecutions: 'Latest executions',
  latestExecutionsDescription: 'The five most recent runs in this project.',
  executionFilter: 'Filter executions',
  snapshot: 'Execution snapshot, updated',
  unnamedExecution: 'Untitled execution',
  unknownDate: 'Time unavailable',
  failedStep: 'Step {number} failed',
  filters: {
    all: 'All',
    failures: 'Failures and errors'
  },
  actions: {
    title: 'Run tests',
    runCase: 'Run case',
    runSuite: 'Run suite',
    createProject: 'Create project',
    createFirstCase: 'Create first case',
    openCases: 'Open test cases',
    viewAllExecutions: 'Case executions',
    viewSuiteReports: 'Suite reports',
    retry: 'Retry',
    refresh: 'Refresh recent executions'
  },
  assets: {
    title: 'Project assets',
    cases: 'Test cases',
    suites: 'Test suites'
  },
  executionKind: {
    case: 'Case',
    suite: 'Suite'
  },
  status: {
    pending: 'Pending',
    running: 'Running',
    passed: 'Passed',
    failed: 'Failed',
    error: 'Error',
    aborted: 'Aborted',
    unknown: 'Unknown'
  },
  states: {
    projectsLoading: 'Loading projects…',
    projectsError: 'Projects could not be loaded.',
    noProjectsTitle: 'Create a UI automation project to get started',
    noProjectsDescription: 'Projects keep test cases, suites, and execution history together.',
    invalidProject: 'This project is unavailable. Select an authorized project to continue.',
    chooseProject: 'Select a project to load its assets and execution history.',
    assetsLoading: 'Loading project assets…',
    assetsError: 'Project assets could not be loaded.',
    executionsLoading: 'Loading recent executions…',
    executionsError: 'Recent executions could not be loaded.',
    noExecutions: 'No executions yet',
    noExecutionsDescription: 'Start by running a case in this project.',
    noFailures: 'No failed executions',
    noFailuresDescription: 'There are no failed or errored runs in this project.',
    activityLoading: 'Loading cross-project activity…',
    activityError: 'Operation history could not be loaded.',
    noActivity: 'No operation history is available.'
  },
  activity: {
    title: 'Operation history',
    crossProject: 'Cross-project history',
    unknownUser: 'A user'
  },
  help: {
    open: 'Help',
    title: 'UI automation quick guide',
    intro: 'Set the project base URL to the page under test. The browser opens this address when the case starts.',
    browserTitle: 'Browser setup',
    browserSetup: 'Choose the engine and browser in the case run settings. Install the selected browser on the execution worker. Use headless mode if the worker has no desktop.',
    exampleTitle: 'Example · Check the page heading',
    exampleDescription: 'Suppose the page has a heading that reads “Sign in”.',
    exampleElement: 'In Elements, add the heading with the CSS Selector strategy and the selector h1.',
    exampleAssertion: 'Create a case and add an Assert step. Pick the heading, select Text contains, and enter Sign in as the expected value.',
    exampleRun: 'Save and run the case to see the assertion result.',
  }
}
