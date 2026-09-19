#!/usr/bin/env python3
"""Regenerate the watchOS recorder and its iPhone pairing companion."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re

ROOT = Path(__file__).resolve().parent


def ident(name):
    return hashlib.sha256(name.encode()).hexdigest()[:24].upper()


def quote(value):
    return json.dumps(value, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--team', default=os.environ.get('HEMORY_DEVELOPMENT_TEAM', ''),
                        help='Your Apple development Team ID; empty leaves signing unconfigured')
    parser.add_argument('--bundle-id', default=os.environ.get('HEMORY_BUNDLE_ID', 'org.example.openhemory'),
                        help='Your unique iPhone bundle ID; Watch uses this ID plus .watchkitapp')
    args = parser.parse_args()
    if args.team and not re.fullmatch(r'[A-Z0-9]{10}', args.team):
        parser.error('--team must be a 10-character Apple Team ID or empty')
    if not re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', args.bundle_id):
        parser.error('--bundle-id must be a reverse-DNS identifier')
    sources = ["HemoryLocalApp.swift", "Storage.swift", "Recorder.swift", "Uploader.swift", "PairingBridge.swift", "../HoldState.swift"]
    objects = []

    def obj(name, text):
        objects.append(f"\t\t{ident(name)} = {{ {text} }};")

    for source in sources:
        obj(source, f"isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {quote(source)}; sourceTree = \"<group>\";")
        obj(source + " build", f"isa = PBXBuildFile; fileRef = {ident(source)};")
    obj("Assets", 'isa = PBXFileReference; lastKnownFileType = folder.assetcatalog; path = Assets.xcassets; sourceTree = "<group>";')
    obj("Assets build", f'isa = PBXBuildFile; fileRef = {ident("Assets")};')
    obj("Privacy", 'isa = PBXFileReference; lastKnownFileType = text.xml; path = PrivacyInfo.xcprivacy; sourceTree = "<group>";')
    obj("Privacy build", f'isa = PBXBuildFile; fileRef = {ident("Privacy")};')
    obj("Info", 'isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = "<group>";')
    obj("product", 'isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = HemoryLocal.app; sourceTree = BUILT_PRODUCTS_DIR;')
    obj("container product", 'isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = HemoryLocalDistribution.app; sourceTree = BUILT_PRODUCTS_DIR;')
    obj("source group", f'isa = PBXGroup; children = ({", ".join(ident(x) for x in sources + ["Info", "Assets", "Privacy"])},); path = HemoryLocal; sourceTree = "<group>";')
    obj("phone source", 'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = ExtBrainPhoneApp.swift; sourceTree = "<group>";')
    obj("mac status client", 'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = MacStatusClient.swift; sourceTree = "<group>";')
    obj("phone assets", 'isa = PBXFileReference; lastKnownFileType = folder.assetcatalog; path = Assets.xcassets; sourceTree = "<group>";')
    obj("phone group", f'isa = PBXGroup; children = ({ident("phone source")}, {ident("mac status client")}, {ident("phone assets")},); path = Phone; sourceTree = "<group>";')
    for item in ["phone source", "mac status client", "phone assets", "Privacy", "Storage.swift", "PairingBridge.swift"]:
        obj(item + " phone build", f'isa = PBXBuildFile; fileRef = {ident(item)};')
    obj("products", f'isa = PBXGroup; children = ({ident("product")}, {ident("container product")},); name = Products; sourceTree = "<group>";')
    obj("main group", f'isa = PBXGroup; children = ({ident("source group")}, {ident("phone group")}, {ident("products")},); sourceTree = "<group>";')
    obj("sources", f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ({", ".join(ident(x + " build") for x in sources)},); runOnlyForDeploymentPostprocessing = 0;')
    obj("frameworks", 'isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;')
    obj("resources", f'isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = ({ident("Assets build")}, {ident("Privacy build")},); runOnlyForDeploymentPostprocessing = 0;')
    obj("target", f'''isa = PBXNativeTarget; buildConfigurationList = {ident("target configs")};
            buildPhases = ({ident("sources")}, {ident("frameworks")}, {ident("resources")},);
            buildRules = (); dependencies = (); name = HemoryLocal; productName = HemoryLocal;
            productReference = {ident("product")}; productType = "com.apple.product-type.application";''')
    # The iPhone companion and Watch bundle identifiers share the configured base.
    obj("embed watch build", f'isa = PBXBuildFile; fileRef = {ident("product")}; settings = {{ ATTRIBUTES = (RemoveHeadersOnCopy,); }};')
    obj("embed watch", f'isa = PBXCopyFilesBuildPhase; buildActionMask = 2147483647; dstPath = "$(CONTENTS_FOLDER_PATH)/Watch"; dstSubfolderSpec = 16; files = ({ident("embed watch build")},); name = "Embed Watch Content"; runOnlyForDeploymentPostprocessing = 0;')
    obj("watch proxy", f'isa = PBXContainerItemProxy; containerPortal = {ident("project")}; proxyType = 1; remoteGlobalIDString = {ident("target")}; remoteInfo = HemoryLocal;')
    obj("watch dependency", f'isa = PBXTargetDependency; target = {ident("target")}; targetProxy = {ident("watch proxy")};')
    obj("container sources", f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ({", ".join(ident(x + " phone build") for x in ["phone source", "mac status client", "Storage.swift", "PairingBridge.swift"])},); runOnlyForDeploymentPostprocessing = 0;')
    obj("container frameworks", 'isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;')
    obj("container resources", f'isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = ({ident("phone assets phone build")}, {ident("Privacy phone build")},); runOnlyForDeploymentPostprocessing = 0;')
    obj("container target", f'''isa = PBXNativeTarget; buildConfigurationList = {ident("container configs")};
            buildPhases = ({ident("container sources")}, {ident("container frameworks")}, {ident("container resources")}, {ident("embed watch")},);
            buildRules = (); dependencies = ({ident("watch dependency")},); name = HemoryLocalDistribution;
            productName = HemoryLocalDistribution; productReference = {ident("container product")};
            productType = "com.apple.product-type.application";''')
    for mode in ["Debug", "Release"]:
        project_settings = {
            "ALWAYS_SEARCH_USER_PATHS": "NO", "CLANG_ENABLE_MODULES": "YES", "CLANG_ENABLE_OBJC_ARC": "YES",
            "ENABLE_STRICT_OBJC_MSGSEND": "YES", "GCC_C_LANGUAGE_STANDARD": "gnu17",
            "SDKROOT": "watchos", "WATCHOS_DEPLOYMENT_TARGET": "10.0",
            "DEBUG_INFORMATION_FORMAT": "dwarf" if mode == "Debug" else "dwarf-with-dsym",
            "SWIFT_OPTIMIZATION_LEVEL": "-Onone" if mode == "Debug" else "-O",
        }
        if mode == "Debug":
            project_settings["SWIFT_ACTIVE_COMPILATION_CONDITIONS"] = "DEBUG $(inherited)"
            project_settings["ENABLE_TESTABILITY"] = "YES"
        else:
            project_settings["SWIFT_COMPILATION_MODE"] = "wholemodule"
        target_settings = {
            "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon",
            "CODE_SIGN_STYLE": "Automatic", "DEVELOPMENT_TEAM": args.team, "CURRENT_PROJECT_VERSION": "5",
            "GENERATE_INFOPLIST_FILE": "NO", "INFOPLIST_FILE": "HemoryLocal/Info.plist",
            "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/Frameworks",
            "MARKETING_VERSION": "0.1.0", "PRODUCT_BUNDLE_IDENTIFIER": args.bundle_id + ".watchkitapp",
            "HEMORY_COMPANION_BUNDLE_IDENTIFIER": args.bundle_id,
            "PRODUCT_NAME": "$(TARGET_NAME)", "SDKROOT": "watchos", "SKIP_INSTALL": "YES",
            "SUPPORTED_PLATFORMS": "watchos watchsimulator", "SWIFT_VERSION": "5.0",
            "SWIFT_STRICT_CONCURRENCY": "targeted", "TARGETED_DEVICE_FAMILY": "4",
            "WATCHOS_DEPLOYMENT_TARGET": "10.0", "ENABLE_USER_SCRIPT_SANDBOXING": "YES",
        }
        container_settings = {
            "CODE_SIGN_STYLE": "Automatic", "DEVELOPMENT_TEAM": args.team,
            "CURRENT_PROJECT_VERSION": "5", "MARKETING_VERSION": "0.1.0",
            "PRODUCT_BUNDLE_IDENTIFIER": args.bundle_id, "PRODUCT_NAME": "$(TARGET_NAME)",
            "SDKROOT": "iphoneos", "SUPPORTED_PLATFORMS": "iphoneos iphonesimulator",
            "IPHONEOS_DEPLOYMENT_TARGET": "17.0", "TARGETED_DEVICE_FAMILY": "1,2",
            "GENERATE_INFOPLIST_FILE": "YES", "INFOPLIST_KEY_CFBundleDisplayName": "ExtBrain",
            "INFOPLIST_KEY_ITSAppUsesNonExemptEncryption": "NO", "SKIP_INSTALL": "NO",
            "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon", "SWIFT_VERSION": "5.0",
            "SWIFT_STRICT_CONCURRENCY": "targeted", "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/Frameworks",
            "INFOPLIST_KEY_UIApplicationSceneManifest_Generation": "YES",
            "INFOPLIST_KEY_UILaunchScreen_Generation": "YES",
            "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPhone": "UIInterfaceOrientationPortrait UIInterfaceOrientationLandscapeLeft UIInterfaceOrientationLandscapeRight",
            "INFOPLIST_KEY_UISupportedInterfaceOrientations_iPad": "UIInterfaceOrientationPortrait UIInterfaceOrientationPortraitUpsideDown UIInterfaceOrientationLandscapeLeft UIInterfaceOrientationLandscapeRight",
        }
        for prefix, settings in [("project", project_settings), ("target", target_settings), ("container", container_settings)]:
            pairs = " ".join(f"{key} = {quote(value)};" for key, value in settings.items())
            obj(prefix + mode, f"isa = XCBuildConfiguration; buildSettings = {{ {pairs} }}; name = {mode};")
    for prefix in ["project", "target", "container"]:
        obj(prefix + " configs", f'isa = XCConfigurationList; buildConfigurations = ({ident(prefix + "Debug")}, {ident(prefix + "Release")},); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;')
    obj("project", f'''isa = PBXProject; attributes = {{ BuildIndependentTargetsInParallel = YES; LastUpgradeCheck = 1600;
            TargetAttributes = {{ {ident("target")} = {{ CreatedOnToolsVersion = 16.0; }}; }}; }};
            buildConfigurationList = {ident("project configs")}; compatibilityVersion = "Xcode 14.0";
            developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en, Base, "zh-Hans");
            mainGroup = {ident("main group")}; productRefGroup = {ident("products")}; projectDirPath = "";
            projectRoot = ""; targets = ({ident("target")}, {ident("container target")},);''')
    project = ROOT / "HemoryLocal.xcodeproj"
    project.mkdir(exist_ok=True)
    text = '// !$*UTF8*$!\n{\n\tarchiveVersion = 1;\n\tclasses = {};\n\tobjectVersion = 56;\n\tobjects = {\n'
    text += "\n".join(objects) + f'\n\t}};\n\trootObject = {ident("project")};\n}}\n'
    (project / "project.pbxproj").write_text(text)
    schemes = project / "xcshareddata" / "xcschemes"
    schemes.mkdir(parents=True, exist_ok=True)
    ref = f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{ident("target")}" BuildableName="HemoryLocal.app" BlueprintName="HemoryLocal" ReferencedContainer="container:HemoryLocal.xcodeproj"/>'
    scheme = f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="1600" version="1.3">
 <BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{ref}</BuildActionEntry></BuildActionEntries></BuildAction>
 <TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB"><Testables/></TestAction>
 <LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES"><BuildableProductRunnable runnableDebuggingMode="0">{ref}</BuildableProductRunnable></LaunchAction>
 <ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{ref}</BuildableProductRunnable></ProfileAction>
 <AnalyzeAction buildConfiguration="Debug"/>
 <ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>
'''
    (schemes / "HemoryLocal.xcscheme").write_text(scheme)
    distribution_ref = f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{ident("container target")}" BuildableName="HemoryLocalDistribution.app" BlueprintName="HemoryLocalDistribution" ReferencedContainer="container:HemoryLocal.xcodeproj"/>'
    (schemes / "HemoryLocalDistribution.xcscheme").write_text(scheme.replace(ref, distribution_ref))
    print(project)


if __name__ == "__main__":
    main()
