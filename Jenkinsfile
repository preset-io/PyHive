LIB_NAME = 'PyHive'
String currentVersion = ""

podTemplate(
    imagePullSecrets: ['preset-pull'],
    nodeUsageMode: 'NORMAL',
    containers: [
        containerTemplate(
            alwaysPullImage: true,
            name: 'ci',
            image: 'preset/ci:latest',
            ttyEnabled: true,
            command: 'cat',
            resourceRequestCpu: '100m',
            resourceLimitCpu: '200m',
            resourceRequestMemory: '1000Mi',
            resourceLimitMemory: '2000Mi',
        ),
        containerTemplate(
            alwaysPullImage: true,
            name: 'py-ci',
            image: 'preset/python:3.10.13-2024-02-21-ci',
            ttyEnabled: true,
            command: 'cat'
        )
    ]
) {
    node(POD_LABEL) {
        container('py-ci') {
            stage('Checkout') {
                checkout scm
            }

            stage('Install System Dependencies') {
                sh 'apt-get update && apt-get install -y libkrb5-dev python3-dev libsasl2-dev'
            }

            stage('Tests') {
                sh(script: 'pip install -e . && pip install -r dev_requirements.txt', label: 'install dependencies')
                parallel(
                    check: {
                        currentVersion = sh(
                                script: "python setup.py --version",
                                returnStdout: true,
                                label: 'Get current version'
                        ).trim()
                        container('ci') {
                            withCredentials([
                                [
                                    $class           : 'AmazonWebServicesCredentialsBinding',
                                    credentialsId    : 'ci-user',
                                    accessKeyVariable: 'AWS_ACCESS_KEY_ID',
                                    secretKeyVariable: 'AWS_SECRET_ACCESS_KEY',
                                ]
                            ]) {
                                def retVal = sh(
                                        script: """
                                            set -eu
                                            if aws s3api head-object --bucket preset-pypi --key '${LIB_NAME}/${LIB_NAME}-${currentVersion}.tar.gz' > /dev/null 2> head-object.err; then
                                                exit 0
                                            fi
                                            if grep -q '(404)' head-object.err; then
                                                exit 3
                                            fi
                                            cat head-object.err >&2
                                            exit 1
                                        """,
                                        returnStatus: true,
                                        label: 'Check for existing tarball via AWS API'
                                )
                                // This is an early version gate, not the atomic write guard.
                                if (retVal == 0) {
                                    error("Version ${currentVersion} of ${LIB_NAME} already exists! Version bump required.")
                                }
                                if (retVal != 3) {
                                    error('Could not check the release in S3; refusing to publish.')
                                }
                            }
                        }
                    }
                )
            }
        }

        container('py-ci') {
            stage('Package Release') {
                if (env.BRANCH_NAME.startsWith("PR-")) {
                    sh(script:"git config --global --add safe.directory /home/jenkins/agent/workspace/preset-io_PyHive_${env.BRANCH_NAME}", label: 'Setting safe directory')
                    def shortGitRev = sh(
                            returnStdout: true,
                            script: 'git rev-parse --short HEAD'
                    ).trim()
                    def pullRequestVersion = "${currentVersion}+${env.BRANCH_NAME}.${shortGitRev}"
                    sh(script:"sed -i 's/__version__ = \"${currentVersion}\"/__version__ = \"${pullRequestVersion}\"/g' pyhive/__init__.py", label: 'Changing version for PR')
                    sh(script:"echo PR version: ${pullRequestVersion}", label: 'PR Release candidate version')
                }
                sh(script: 'rm -rf dist && python setup.py sdist --formats=gztar', label: 'Bundling release')
                sh(script: "mkdir -p dist/${LIB_NAME} && mv dist/*.gz dist/${LIB_NAME}", label: 'Setup release folder')
            }
        }

        container('ci') {
            stage('Upload Release') {
                withCredentials([
                    [
                        $class           : 'AmazonWebServicesCredentialsBinding',
                        credentialsId    : 'ci-user',
                        accessKeyVariable: 'AWS_ACCESS_KEY_ID',
                        secretKeyVariable: 'AWS_SECRET_ACCESS_KEY',
                    ]
                ]) {
                    if ((env.BRANCH_NAME == 'master') || (env.BRANCH_NAME.startsWith("PR-"))) {
                        // Match the Drill publisher: CLI v1 cannot express If-None-Match.
                        // S3 rejects an existing key atomically, including concurrent writes.
                        sh(
                            script: '''
                                set -eu
                                python -m pip install --quiet 'boto3>=1.36,<2'
                                for artifact in dist/PyHive/*.tar.gz; do
                                    test -f "$artifact"
                                    key="${artifact#dist/}"
                                    BUCKET='preset-pypi' KEY="$key" ARTIFACT="$artifact" \
                                      python -c 'import os, boto3; artifact = open(os.environ["ARTIFACT"], "rb"); boto3.client("s3").put_object(Bucket=os.environ["BUCKET"], Key=os.environ["KEY"], Body=artifact, IfNoneMatch="*")'
                                    aws s3api get-object --bucket preset-pypi --key "$key" stored.tar.gz > /dev/null
                                    cmp "$artifact" stored.tar.gz
                                    rm stored.tar.gz
                                done
                            ''',
                            label: 'Upload without overwrite and verify stored tarball'
                        )
                    }
                    else {
                        echo "Skipping upload as this isn't master..."
                    }
                }
            }

            stage('Tag Release') {
                if (env.BRANCH_NAME == 'master') {
                    sshagent(credentials: ['gh-preset-machine-ssh-pk']) {
                        sh("git config --global --add safe.directory '*'")
                        sh("git config user.email 'ci@preset.io'")
                        sh("git config user.name 'Jenkins CI'")
                        sh("git tag -a v${currentVersion} -m 'Release v${currentVersion}'")
                        sh("GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git push git@github.com:preset-io/PyHive.git v${currentVersion}")
                    }
                }
            }
        }
    }
}
