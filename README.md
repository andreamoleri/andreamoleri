```swift
import Cocoa

class SoftwareEngineer {
    let name = "Andrea Moleri"
    let role = "Software Engineer"
    let spokenLanguages = ["🇮🇹", "🇺🇸", "🇪🇸", "🇩🇪"]
    let areasOfInterest = ["Queue Theory", "Large Scale Crowd Management"]
    let specializations = ["Machine Learning", "iOS Development", "Software Architecture"]
    
    func greetings() {
        print("Hi there, thanks for dropping by! Hope you find something intersting")
    }
}

let me = SoftwareEngineer()
me.greetings()
```
