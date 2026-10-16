// The cw scan as a DAG (specs/workflow-engine.md): each step is `job/cw-step.sh STEP` in the scan image, run as its
// own Batch task once its inputs exist. Steps hand off through the data bucket (`$GCS/$DATA/…`, the work dir's bucket,
// FUSE-mounted in every task); the channels below carry only `done` markers, which Nextflow's `-resume` keys on.
// The head (`job/nf/cw-head.sh`) starts and finishes the scan-run record and resumes a failed scan's session.
nextflow.enable.dsl = 2

params.scan = null           // SNAP_ID
params.gen = null            // the index generation, stamped once by the head
params.job_start = null      // DT_JOB_START (epoch s)
params.run_id = ''           // DT_RUN_ID: the head job's uid, so every task records into one run
params.buckets = 'marin-us-east-02a hero-checkpoints marin-us-east-06a rhoarnet-us-east-08a marin-us-west-04a'
params.big = 'marin-us-east-02a'    // the buckets listed on the big machine (label `list_big`)
params.secrets = ''          // `-s ENV=SECRET` bindings for `dt-cloud secrets exec`, space-separated
params.env = [:]             // extra env for every step (DATA_BUCKET, R2_BUCKET, STATIC_NAMES, …)
params.machines = [list_big: 'n2-standard-8', list_small: 'e2-standard-4']

def stepEnv() {
    def e = [SNAP_ID: params.scan, GEN: params.gen, DT_JOB_START: params.job_start, DT_RUN_ID: params.run_id,
             GCS: '/mnt/disks', WORK_DIR: '/tmp/cw', APP_DIR: '/app'] + params.env
    e.collect { k, v -> "export ${k}='${v}'" }.join('\n')
}

def step(String name, String arg = '') {
    def sec = params.secrets.tokenize(' ').collect { "-s ${it}" }.join(' ')
    """
    ${stepEnv()}
    dt-cloud secrets exec ${sec} -- bash /app/job/cw-step.sh ${name} ${arg}
    echo ${name} ${arg} > done
    """
}

process bucket_check {
    label 'small'
    output: path 'done'
    script: step('bucket-check')
}

process scan_bucket {
    tag "${bucket}"
    label 'list'
    machineType { bucket in params.big.tokenize(' ') ? params.machines.list_big : params.machines.list_small }
    input: val bucket
    output: path 'done'
    script: step('scan-bucket', bucket)
}

process index {
    label 'big'
    input: path 'scanned/done*'
    output: path 'done'
    script: step('index')
}

process static_names {
    label 'small'
    time '6h'
    input: path 'indexed'
    output: path 'done'
    script: step('static-names')
}

process publish_r2 {
    label 'medium'
    input: path 'indexed'
    output: path 'done'
    script: step('publish-r2')
}

process interval_store {
    label 'big'
    input: path 'published'
    output: path 'done'
    script: step('interval-store')
}

process index_sync {
    label 'small'
    input:
    path 'static'
    path 'intervals'
    output: path 'done'
    script: step('index-sync')
}

process post {
    label 'post'
    input: path 'listed'
    output: path 'done'
    script:
    ['over-time', 'index-gc', 'warm-cache', 'probe', 'digest'].collect { step(it) }.join('\n')
}

workflow {
    if (!params.scan || !params.gen || !params.job_start) error "--scan, --gen and --job_start are required (job/nf/cw-head.sh sets them)"
    bucket_check()
    scanned = scan_bucket(Channel.fromList(params.buckets.tokenize(' '))).collect()
    indexed = index(scanned)
    st = static_names(indexed)
    iv = interval_store(publish_r2(indexed))
    post(index_sync(st, iv))
}
