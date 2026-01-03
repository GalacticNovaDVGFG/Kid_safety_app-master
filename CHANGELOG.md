# Changelog

## Unreleased

### Fixes

- Fix: use React 18 to satisfy @testing-library/react peerDependency (updated `react`, `react-dom`, and `@types/*`)  
- Chore: update `package-lock.json` after dependency changes  
- Fix(guardian): use `stopMedia()` in cleanup (replaced undefined `stopCamera`)  
- Fix(native-fcm): use eval-import to avoid bundler resolving native plugin on web builds  
- Fix(guardian): correct `isCalling` -> `isCalling911` typo  
- Fix(guardian): remove nonexistent `stopRecording` import and stop recorder directly  

These changes resolve Vercel build failures caused by mismatched React versions and bundler resolution of native plugins. The project builds successfully locally after these fixes.
