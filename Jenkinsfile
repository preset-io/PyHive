LIB_NAME = 'PyHive'
String currentVersion = ""
// Normalized sdist filename for currentVersion; see scripts/release_artifact.py.
String currentArtifact = ""
// Normalized sdist filename actually built (PR builds carry a local version).
String releaseArtifact = ""

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
                sh(script: "pip install -e . && pip install -r dev_requirements.txt && pip install packaging 'setuptools>=69.3'", label: 'install dependencies')
                sh(
                    script: '''
                        set -eu
                        python -m venv /tmp/unit
                        /tmp/unit/bin/pip install --quiet -e '.[presto,sqlalchemy,hive_pure_sasl]' 'sqlalchemy>=2.0,<2.1' 'pytest>=8,<9' mock packaging 'setuptools>=69.3'
                        # Offline suites only: the other pyhive/tests modules need live servers.
                        PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /tmp/unit/bin/python -m pytest -c /dev/null --rootdir . -q \
                            pyhive/tests/test_common.py pyhive/tests/test_presto_types.py scripts/test_release_artifact.py
                    ''',
                    label: 'Offline unit tests'
                )
                parallel(
                    check: {
                        currentVersion = sh(
                                script: "python setup.py --version",
                                returnStdout: true,
                                label: 'Get current version'
                        ).trim()
                        currentArtifact = sh(
                                script: "python scripts/release_artifact.py sdist '${LIB_NAME}' '${currentVersion}'",
                                returnStdout: true,
                                label: 'Get normalized release filename'
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
                                            if aws s3api head-object --bucket preset-pypi --key '${LIB_NAME}/${currentArtifact}' > /dev/null 2> head-object.err; then
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
                String releaseVersion = currentVersion
                if (env.BRANCH_NAME.startsWith("PR-")) {
                    sh(script:"git config --global --add safe.directory /home/jenkins/agent/workspace/preset-io_PyHive_${env.BRANCH_NAME}", label: 'Setting safe directory')
                    def shortGitRev = sh(
                            returnStdout: true,
                            script: 'git rev-parse --short HEAD'
                    ).trim()
                    releaseVersion = sh(
                            returnStdout: true,
                            script: "python scripts/release_artifact.py version '${currentVersion}' '${env.BRANCH_NAME}' '${shortGitRev}'",
                            label: 'Normalize PR version'
                    ).trim()
                    sh(script:"sed -i 's/__version__ = \"${currentVersion}\"/__version__ = \"${releaseVersion}\"/g' pyhive/__init__.py", label: 'Changing version for PR')
                    sh(script:"echo PR version: ${releaseVersion}", label: 'PR Release candidate version')
                }
                releaseArtifact = sh(
                        returnStdout: true,
                        script: "python scripts/release_artifact.py sdist '${LIB_NAME}' '${releaseVersion}'",
                        label: 'Get normalized release filename'
                ).trim()
                sh(script: 'rm -rf dist && python setup.py sdist --formats=gztar', label: 'Bundling release')
                // Fail here if the build tool named the artifact differently from the checked key.
                sh(script: "test -f 'dist/${releaseArtifact}' && mkdir -p dist/${LIB_NAME} && mv 'dist/${releaseArtifact}' dist/${LIB_NAME}/", label: 'Setup release folder')
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
                        withEnv(["RELEASE_KEY=${LIB_NAME}/${releaseArtifact}"]) {
                        sh(
                            script: '''
                                set -eu
                                python -m pip install --quiet 'boto3>=1.36,<2'
                                # The same normalized name the existence check used.
                                artifact="dist/$RELEASE_KEY"
                                test -f "$artifact"
                                BUCKET='preset-pypi' KEY="$RELEASE_KEY" ARTIFACT="$artifact" \
                                  python -c 'import os, boto3; artifact = open(os.environ["ARTIFACT"], "rb"); boto3.client("s3").put_object(Bucket=os.environ["BUCKET"], Key=os.environ["KEY"], Body=artifact, IfNoneMatch="*")'
                                aws s3api get-object --bucket preset-pypi --key "$RELEASE_KEY" stored.tar.gz > /dev/null
                                cmp "$artifact" stored.tar.gz
                                rm stored.tar.gz
                            ''',
                            label: 'Upload without overwrite and verify stored tarball'
                        )
                        }
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
